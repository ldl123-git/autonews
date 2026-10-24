"""成稿交付：把每个平台版本导出到 output_finish/<平台>/<标题>/。

规则：
* 一个平台一个目录，目录名用该平台版标题（tt=头条 / bjh=百家）；
* 插入多少素材由文章长短决定（见 layout.plan_counts），人工审核过的排版优先；
* 采集到的素材**全部**拷进 素材/图片、素材/视频，供排版页增删换；
* Markdown 里写相对路径，整个目录可以直接打包/挪盘；
* 幂等：同一篇文章重跑会覆盖同名目录里的文件（别的文章同标题才加 -id 后缀）。
"""
import datetime as dt
import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import exporter, layout, sensitive, settings, style
from .config import FINISH_COPY_ASSETS, FINISH_DIR, FINISH_IMG_MAX, FINISH_VIDEO_MAX
from .db import get_conn, write_conn

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe(text: Any, limit: int = 40) -> str:
    """目录/文件名安全化：去非法字符、压空白、超长截断。"""
    s = _ILLEGAL.sub(" ", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    if len(s) > limit:
        s = s[:limit]
    return s.rstrip(". 。，,、；;：:-—") or "未命名"


# ---------------------------------------------------------------- 取版本 / 素材
def _form_of(value: Any) -> str:
    """老数据没有 content_form：按图文处理。"""
    return str(value or "article")


def _pick_rewrite(article_id: int, platform: str, form: str = "article") -> Optional[Dict[str, Any]]:
    """只取该平台 · 形态自己的版本。

    图文与短稿（微头条 / 百家动态）是两条独立记录：拿不到目标形态宁可报错，
    也不能把图文稿当短稿发（那会带着标题、篇幅也不对）。原文短时只有短稿记录，
    图文那条本来就是空的，不去补。老数据（target_platform 为空）只在找图文时兜底。
    """
    form = _form_of(form)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM article_rewrites WHERE article_id=? AND target_platform=?"
            " AND COALESCE(NULLIF(content_form,''),'article')=? AND status='success'"
            " ORDER BY id DESC LIMIT 1", (article_id, platform, form)).fetchone()
        if row is None and form == "article":
            row = conn.execute(
                "SELECT * FROM article_rewrites WHERE article_id=? AND (target_platform IS NULL"
                " OR target_platform='') AND status='success' ORDER BY id DESC LIMIT 1",
                (article_id,)).fetchone()
    return dict(row) if row else None


def _pick_layout(article_id: int, platform: str, rewrite_id: Any,
                 form: str = "article") -> Optional[Dict[str, Any]]:
    """取人工审核过的排版方案：同平台同形态 + 同改写版本优先，其次最新一条。"""
    form = _form_of(form)
    with get_conn() as conn:
        row = None
        if rewrite_id:
            row = conn.execute(
                "SELECT id FROM article_layouts WHERE article_id=? AND rewrite_id=?"
                " AND platform=? AND COALESCE(NULLIF(content_form,''),'article')=?"
                " ORDER BY id DESC LIMIT 1",
                (article_id, rewrite_id, platform, form)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM article_layouts WHERE article_id=? AND platform=?"
                " AND COALESCE(NULLIF(content_form,''),'article')=?"
                " ORDER BY id DESC LIMIT 1", (article_id, platform, form)).fetchone()
    return layout.detail(row["id"]) if row else None


ASSET_DIR = "素材"
IMG_SUB = "图片"
VID_SUB = "视频"


def _copy_assets(images: List[Dict[str, Any]], videos: List[Dict[str, Any]],
                 box: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把**全部**素材拷进交付目录，返回 path 换成相对路径后的素材列表。

    全量拷贝的目的：交付目录自成一体（可整体打包 / 挪盘），并且人工审核时
    可以在已拷进去的素材里增、删、换，不必再回 output 目录找文件。
    每次导出前先清掉旧的 images/video/素材 目录，避免被删素材的残留文件。
    """
    if not FINISH_COPY_ASSETS:
        return images, videos
    for legacy in ("images", "video", ASSET_DIR):
        target = box / legacy
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    img_dir = box / ASSET_DIR / IMG_SUB
    vid_dir = box / ASSET_DIR / VID_SUB
    img_dir.mkdir(parents=True, exist_ok=True)
    vid_dir.mkdir(parents=True, exist_ok=True)

    out_img: List[Dict[str, Any]] = []
    for i, a in enumerate(images, 1):
        src = Path(str(a.get("path") or ""))
        if not src.exists():
            continue
        label = a.get("title") or a.get("keyword") or "image"
        if a.get("from_frame"):
            label = f"抽帧-{label}"          # 交付目录里一眼看出这张是视频截图
        name = f"{i:02d}-{_safe(label, 24)}{src.suffix or '.jpg'}"
        try:
            shutil.copy2(src, img_dir / name)
        except OSError:
            continue
        out_img.append(dict(a, path=f"./{ASSET_DIR}/{IMG_SUB}/{name}"))

    out_vid: List[Dict[str, Any]] = []
    for i, a in enumerate(videos, 1):
        src = Path(str(a.get("path") or ""))
        if not src.exists():
            continue
        name = f"{i:02d}-{_safe(a.get('title') or a.get('keyword') or 'video', 24)}" \
               f"{src.suffix or '.mp4'}"
        try:
            shutil.copy2(src, vid_dir / name)
        except OSError:
            continue
        out_vid.append(dict(a, path=f"./{ASSET_DIR}/{VID_SUB}/{name}"))
    return out_img, out_vid


def _box_dir(platform: str, form: str, title: str, article_id: int) -> Path:
    """交付目录：图文沿用 tt / bjh，短稿单独放 tt-wtt（微头条）/ bjh-dt（百家动态）。"""
    base = FINISH_DIR / style.dir_of(platform, form)
    base.mkdir(parents=True, exist_ok=True)
    name = exporter.safe_dirname(title)
    target = base / name
    meta_file = target / "meta.json"
    if meta_file.exists():
        try:
            old = json.loads(meta_file.read_text(encoding="utf-8"))
            if int(old.get("article_id") or -1) != int(article_id):
                target = base / f"{name}-{article_id}"
        except (OSError, ValueError):
            target = base / f"{name}-{article_id}"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _plain(title: str, md: str) -> str:
    """Markdown → 纯文本：去掉图片/视频行与标记，方便直接粘到后台编辑器。"""
    lines: List[str] = []
    for raw in str(md or "").splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        if line.startswith("![") or line.startswith("<video") or line.startswith("> 视频："):
            continue
        if line.startswith("*") and line.endswith("*") and len(line) > 2:
            continue                      # 图注
        lines.append(re.sub(r"^#{1,6}\s*", "", line))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return (text or str(title or "")) + "\n"


# ---------------------------------------------------------------- 单平台导出
def _export_platform(article_id: int, article: Dict[str, Any],
                     platform: str, form: str = "article") -> Dict[str, Any]:
    form = _form_of(form)
    label = style.label_of(platform, form)
    rw = _pick_rewrite(article_id, platform, form)
    if not rw:
        # 有种情况不是「没改写」，而是改写被敏感词判失败了：这时要给准确原因，
        # 否则页面上只会显示「还没有改写稿」，用户找不到人工放行的入口。
        with get_conn() as conn:
            bad = conn.execute(
                "SELECT sensitive_hits FROM article_rewrites WHERE article_id=?"
                " AND target_platform=? AND COALESCE(NULLIF(content_form,''),'article')=?"
                " AND sensitive_status IN ('blocked','waived') ORDER BY id DESC LIMIT 1",
                (article_id, platform, form)).fetchone()
        if bad:
            try:
                words = [str(h.get("word")) for h in json.loads(bad["sensitive_hits"] or "[]")][:8]
            except (TypeError, ValueError):
                words = []
            return {"platform": platform, "content_form": form, "platform_title": label,
                    "ok": False,
                    "error": "敏感词未通过，未交付：" + ("、".join(words) or "见改写详情")
                             + "（可在改写详情里人工放行或重新改写）"}
        return {"platform": platform, "content_form": form, "platform_title": label,
                "ok": False,
                "error": f"还没有{label}的改写稿，先改写该平台版本再交付"}
    form = _form_of(rw.get("content_form") or form)
    label = style.label_of(platform, form)
    need_title = style.need_title(platform, form)
    body = str(rw.get("final_text") or rw.get("draft_text") or "").strip()
    if not body:
        return {"platform": platform, "content_form": form, "platform_title": label,
                "ok": False, "error": "改写稿正文为空"}

    title = str(rw.get("used_title") or rw.get("origin_title")
                or article.get("title") or "") if need_title else ""
    folder_title = str(article.get("title") or title
                       or f"{style.form_title(platform, form)}-{article_id}")

    # 人工审核过的排版方案优先（同平台同形态 + 同改写版本），没有才按文章长短自动排
    reviewed = _pick_layout(article_id, platform, rw.get("id"), form)
    if reviewed and reviewed.get("title") and need_title:
        title = str(reviewed["title"])

    # 交付闸门：成品目录只落合格稿。改写链路已经校验过一轮，这里再查一次，
    # 挡住历史数据、人工改稿、接口直导等旁路（正文可能已经被人手动改过）。
    # 人工放行的稿（sensitive_status='waived'）照常交付，但会把命中词写进 meta 留痕。
    report: Dict[str, Any] = {}
    waived = str(rw.get("sensitive_status") or "") == "waived"
    if settings.get_bool("sensitive.enabled", True):
        report = sensitive.check(body, title if need_title else "")
        # 通过时只刷新状态，保留改写链路记下的命中明细（含首检命中的词，页面上要看）
        with write_conn() as conn:
            if report["ok"]:
                conn.execute(
                    "UPDATE article_rewrites SET sensitive_status='pass', sensitive_checked_at=?"
                    " WHERE id=?", (_now(), rw.get("id")))
            elif not waived:
                conn.execute(
                    "UPDATE article_rewrites SET sensitive_status='blocked', sensitive_hits=?,"
                    " sensitive_checked_at=? WHERE id=?",
                    (json.dumps(report.get("hits") or [], ensure_ascii=False),
                     _now(), rw.get("id")))
        if not report["ok"] and not waived and settings.get_bool("sensitive.block_export", True):
            return {"platform": platform, "content_form": form, "platform_title": label,
                    "ok": False,
                    "error": f"敏感词未通过，未交付：{sensitive.brief(report)}"}

    box = _box_dir(platform, form, folder_title, article_id)
    all_images, all_videos = layout.all_assets(article_id)
    images, videos = _copy_assets(all_images, all_videos, box)
    by_id = {int(a["id"]): a for a in images + videos}

    if reviewed:
        blocks = reviewed.get("blocks") or layout.parse_blocks(body)
        placements = [dict(p) for p in (reviewed.get("placements") or [])
                      if p.get("enabled") and int(p.get("asset_id") or 0) in by_id]
        plan = reviewed.get("plan") or {}
    else:
        blocks = layout.parse_blocks(body)
        if title and blocks and blocks[0]["type"].startswith("h") \
                and blocks[0]["text"] and blocks[0]["text"] in title:
            blocks.pop(0)                 # 模型常把标题又抄一遍当首个小标题
        counts = layout.plan_counts(blocks, body, len(images), len(videos))
        counts["images"] = min(counts["images"], FINISH_IMG_MAX)
        counts["videos"] = min(counts["videos"], FINISH_VIDEO_MAX)
        if form == "micro":               # 短稿（微头条 / 百家动态）版面短：图最多 3 张、不配视频
            counts["images"] = min(counts["images"], 3)
            counts["videos"] = 0
        placements = layout.suggest_placements(
            blocks, layout.pick_images(images, counts["images"]), videos[:counts["videos"]])
        plan = counts

    selected = {int(p.get("asset_id") or 0) for p in placements}
    chosen = [by_id[i] for i in selected if i in by_id]
    md = layout.render_md(title, blocks, placements, images + videos, form=form)

    (box / "文章.md").write_text(md, encoding="utf-8")
    (box / "文章.txt").write_text(_plain(title or folder_title, md), encoding="utf-8")
    meta = {
        "article_id": article_id,
        "rewrite_id": rw.get("id"),
        "platform": platform,
        "platform_title": label,
        "content_form": form,
        "title": title,
        "folder_title": folder_title,
        "source": article.get("source") or "",
        "source_norm": article.get("source_norm") or "",
        "plan": plan,
        # 全部素材都拷进目录并登记，selected 标记哪几个真的插进了正文
        "assets": [{"id": a.get("id"), "kind": a.get("kind"), "match": a.get("match"),
                    "title": a.get("title"), "provider": a.get("provider"),
                    "source": a.get("src_url"), "file": a.get("path"),
                    "from_frame": bool(a.get("from_frame")),
                    "selected": int(a.get("id") or 0) in selected}
                   for a in images + videos],
        "images": [{"id": a.get("id"), "match": a.get("match"),
                    "src": a.get("src_url"), "file": a.get("path"),
                    "from_frame": bool(a.get("from_frame"))} for a in chosen
                   if a.get("kind") == "image"],
        "videos": [{"id": a.get("id"), "match": a.get("match"),
                    "src": a.get("src_url"), "file": a.get("path")} for a in chosen
                   if a.get("kind") == "video"],
        # 敏感词留痕：成品目录里也记一份，人工放行的稿子事后可追溯
        "sensitive": {
            "status": ("waived" if waived and not report.get("ok")
                       else ("pass" if report.get("ok") else "blocked")),
            "waived_at": rw.get("sensitive_waived_at") or "",
            "waived_note": rw.get("sensitive_waived_note") or "",
            "hits": [h.get("word") for h in (report.get("blocked") or [])],
        },
        "exported_at": _now(),
    }
    (box / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    rel = box.relative_to(FINISH_DIR).as_posix()
    return {"platform": platform, "content_form": form, "platform_title": label,
            "ok": True, "dir": rel, "path": str(box), "title": title,
            "images": sum(1 for a in chosen if a.get("kind") == "image"),
            "videos": sum(1 for a in chosen if a.get("kind") == "video"),
            "assets": len(images) + len(videos)}


# ---------------------------------------------------------------- 主入口
def _forms_for_article(article_id: int, platform: str) -> List[str]:
    """这篇在该平台实际产出了哪些形态。

    短稿（原文 <400 字）只有 micro，长文只有 article，所以这里按库里实际记录导出，
    不会去补另一半空形态。没有记录（老数据）时按图文试一次，避免直接判空。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT COALESCE(NULLIF(content_form,''),'article') AS form"
            " FROM article_rewrites WHERE article_id=? AND target_platform=? AND status='success'",
            (article_id, platform)).fetchall()
    forms = [str(r["form"] or "article") for r in rows]
    if not forms:
        return ["article"]
    return [f for f in ("article", "micro") if f in forms] or forms


def export_article(article_id: int) -> Dict[str, Any]:
    """把这篇的每个平台 · 形态都导出（谁后完成谁触发，可重复调用）。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not row:
        return {"article_id": article_id, "ok": False, "error": "文章不存在"}
    article = dict(row)
    # 还没有合格改写稿时不交付：否则必然以「改写稿正文为空」收场，
    # 还会把 finish_status 写成 failed，让文章平白落到流水线异常列。
    with get_conn() as conn:
        has_rw = conn.execute(
            "SELECT 1 FROM article_rewrites WHERE article_id=? AND status='success' LIMIT 1",
            (article_id,)).fetchone()
    if not has_rw:
        return {"article_id": article_id, "ok": False,
                "error": "还没有合格改写稿，暂不交付", "items": []}
    targets = style.targets_for(article.get("source"))

    with write_conn() as conn:
        conn.execute("UPDATE articles SET finish_status='running' WHERE id=?", (article_id,))
    items: List[Dict[str, Any]] = []
    for platform in targets:
        for form in _forms_for_article(article_id, platform):
            items.append(_export_platform(article_id, article, platform, form))
    ok = any(i.get("ok") for i in items)
    dirs = [i["dir"] for i in items if i.get("ok")]
    with write_conn() as conn:
        conn.execute(
            "UPDATE articles SET finish_status=?, finish_at=?, finish_dir=?, updated_at=?"
            " WHERE id=?",
            ("success" if ok else "failed", _now(), ",".join(dirs), _now(), article_id))
    return {"article_id": article_id, "ok": ok, "items": items}


def status(article_id: int) -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, source, source_norm, finish_status, finish_at, finish_dir,"
            " sensitive_status FROM articles WHERE id=?", (article_id,)).fetchone()
    return dict(row) if row else {}


# ---------------------------------------------------------------- 清理已交付成品
def _safe_dirs(raw: Any) -> List[Path]:
    """finish_dir 里逗号分隔的相对目录 → 绝对路径（必须在 FINISH_DIR 内部）。"""
    out: List[Path] = []
    try:
        root = FINISH_DIR.resolve()
    except OSError:
        return out
    for part in str(raw or "").split(","):
        part = part.strip().replace("\\", "/")
        if not part:
            continue
        try:
            target = (FINISH_DIR / part).resolve()
        except OSError:
            continue
        if target != root and root in target.parents:
            out.append(target)
    return out


def _dir_size(path: Path) -> int:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def clear(article_id: int) -> Dict[str, Any]:
    """删除这篇文章的已交付成品（output_finish 目录 + 状态），文章与素材保留。"""
    with get_conn() as conn:
        row = conn.execute("SELECT finish_dir FROM articles WHERE id=?", (article_id,)).fetchone()
    if not row:
        return {"article_id": article_id, "dirs": 0, "freed_bytes": 0}
    freed = removed = 0
    for target in _safe_dirs(row["finish_dir"]):
        if target.exists():
            freed += _dir_size(target)
            shutil.rmtree(target, ignore_errors=True)
            removed += 1
    with write_conn() as conn:
        conn.execute(
            "UPDATE articles SET finish_status='', finish_at='', finish_dir='',"
            " updated_at=? WHERE id=?", (_now(), article_id))
    return {"article_id": article_id, "dirs": removed, "freed_bytes": freed}


def clear_all(ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """批量清理已交付成品：不传 ids 表示清掉所有 finish_status='success' 的。"""
    with get_conn() as conn:
        if ids:
            q = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT id FROM articles WHERE id IN ({q}) AND finish_status='success'",
                list(ids)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM articles WHERE finish_status='success'").fetchall()
    target_ids = [int(r["id"]) for r in rows]
    freed = removed = 0
    for aid in target_ids:
        res = clear(aid)
        freed += int(res.get("freed_bytes") or 0)
        removed += int(res.get("dirs") or 0)
    return {"articles": len(target_ids), "dirs": removed, "freed_bytes": freed}
