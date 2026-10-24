"""文章排版：把改写稿与采集到的素材合成可发布的成稿。

改写只负责「文字像人写的」，排版负责「版面像人排的」：
1. 拆块：识别改写稿里的小标题与正文段落；
2. 定数量：按文章长短决定插几张图 / 几个视频（短文少、长文多，见 plan_counts）；
3. 挑素材：按 media_assets.match（与原文标题的相关度）取最靠前的若干张图/视频；
   视频抽帧与正式图片同池排序（同一视频最多取 MEDIA_FRAME_PER_VIDEO 张），
   同时把全部素材放进快照，供排版页人工审核时增删换；
4. 定位置：在小标题后、段落边界上给出插入点，并说明为什么放这里；
5. 出稿：渲染成 Markdown，可手动开关/换素材后再导出。
"""
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, exporter, sensitive, settings, style
from .db import get_conn, write_conn


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_HEADING = re.compile(r"^\s{0,3}(#{2,6})\s+(.+?)\s*$")
_BOLD_LINE = re.compile(r"^\*\*(.+?)\*\*$")


# ---------------------------------------------------------------- 正文拆块
def _parse(md: str, detect_inline: bool) -> List[Dict[str, str]]:
    blocks: List[Dict[str, str]] = []
    for raw in str(md or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _HEADING.match(line)
        if m:
            blocks.append({"type": f"h{len(m.group(1))}", "text": m.group(2).strip()})
            continue
        if detect_inline:
            mb = _BOLD_LINE.match(line)
            if mb:
                blocks.append({"type": "h2", "text": mb.group(1).strip()})
                continue
            # 短行、不以句末标点收尾、不是列表/引用 → 疑似小标题
            if len(line) <= 24 and not line.endswith(("。", "！", "？", "，", ".", "!", "?", ",")) \
                    and not line.startswith(("-", "*", ">", "[", "!")):
                blocks.append({"type": "h2", "text": line})
                continue
        blocks.append({"type": "p", "text": line})
    return blocks


#: 人工编辑允许的块类型（h1 是标题，不进正文）
_EDIT_TYPES = ("h2", "h3", "h4", "p")


def clean_blocks(raw: Any) -> List[Dict[str, str]]:
    """人工编辑后的块：只认正文/小标题，去首尾空白、丢掉空块。"""
    out: List[Dict[str, str]] = []
    for b in raw or []:
        if isinstance(b, str):
            b = {"type": "p", "text": b}
        if not isinstance(b, dict):
            continue
        typ = str(b.get("type") or "p").strip().lower()
        if typ not in _EDIT_TYPES:
            typ = "p"
        text = str(b.get("text") or "").strip()
        if not text:
            continue
        out.append({"type": typ, "text": text})
    return out


def blocks_to_text(blocks: List[Dict[str, str]]) -> str:
    """块还原成 Markdown 文本（写回改写稿用，parse_blocks 能原样拆回来）。"""
    lines: List[str] = []
    for b in blocks:
        typ = str(b.get("type") or "p")
        text = str(b.get("text") or "")
        if typ.startswith("h"):
            try:
                lines.append(f"{'#' * max(2, min(6, int(typ[1:])))} {text}")
            except ValueError:
                lines.append(text)
        else:
            lines.append(text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def parse_blocks(md: str) -> List[Dict[str, str]]:
    """把改写稿拆成小标题 / 段落块。

    小标题误判比漏判更伤版面，所以识别出的标题比段落还多时自动回退成「全当正文」。
    """
    if not settings.get_bool("layout.detect_inline_title", True):
        return _parse(md, False)
    blocks = _parse(md, True)
    heads = sum(1 for b in blocks if b["type"].startswith("h"))
    paras = sum(1 for b in blocks if b["type"] == "p")
    if heads and heads > paras:
        return _parse(md, False)
    return blocks


# ---------------------------------------------------------------- 挑素材
def _asset_file_ok(item: Dict[str, Any]) -> bool:
    p = Path(item.get("path") or "")
    return bool(p) and p.exists()


def _frames_enabled() -> bool:
    """视频抽帧是否参与排版图片池（默认开，系统设置里可关）。"""
    return settings.get_bool("layout.allow_frames",
                             bool(getattr(config, "MEDIA_FRAME_AS_IMAGE", True)))


def _frame_source(a: Dict[str, Any]) -> str:
    """同一段视频的抽帧归一组：优先视频页地址，退到视频标题。"""
    return str(a.get("page_url") or a.get("src_url") or a.get("title") or a.get("id") or "")


def frame_source(a: Dict[str, Any]) -> str:
    """公开版：给 publish/clipboard.py 用，避免复制一份私有规则。"""
    return _frame_source(a)


def video_frames(assets: List[Dict[str, Any]], video: Dict[str, Any]) -> List[Dict[str, Any]]:
    """这个视频自己的抽帧（按 _frame_source 分组；没有就返回空）。"""
    key = _frame_source(video)
    if not key:
        return []
    return [a for a in assets
            if (a.get("from_frame") or a.get("kind") == "frame")
            and _frame_source(a) == key]



def _image_rank(a: Dict[str, Any]) -> float:
    """图片用于排序的相关度：抽帧按 MEDIA_FRAME_WEIGHT 折算，避免同分时挤掉正式图片。"""
    score = float(a.get("match") or 0)
    if a.get("from_frame"):
        score *= float(getattr(config, "MEDIA_FRAME_WEIGHT", 0.9) or 1.0)
    return score


def all_assets(article_id: int) -> Tuple[List[Dict], List[Dict]]:
    """该文章全部可用素材（图片 + 视频抽帧 / 视频），按相关度排序。

    排版页要把「没排上的素材」也展示出来供人工增删换，所以这里不做截断；
    真正插入几张由 plan_counts 决定。视频抽帧默认并入图片池：视频画面往往比
    搜索来的配图更贴题，按折算后的相关度与正式图片一起排序，from_frame 标记
    供页面区分「这是抽帧」。同一视频的重复帧由 pick_images 限额。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM media_assets WHERE article_id=? AND status='ready'"
            " ORDER BY match DESC, id ASC", (article_id,)
        ).fetchall()
    items = [d for d in (dict(r) for r in rows) if _asset_file_ok(d)]
    images = [x for x in items if x["kind"] == "image"]
    frames = [x for x in items if x["kind"] == "frame"]
    videos = [x for x in items if x["kind"] == "video"]
    # 抽帧始终进快照（排版页可以手动插入）；开关只决定它是否参与自动排序
    frames = [dict(f, kind="image", from_frame=True) for f in frames]
    if _frames_enabled():
        images += frames
        images.sort(key=lambda a: (-_image_rank(a), int(a.get("id") or 0)))
    else:
        images += frames                 # 关掉混排：抽帧只作兜底，排在正式图片后面
    return images, videos


def pick_images(images: List[Dict], count: int) -> List[Dict]:
    """按相关度取 count 张图，抽帧与正式图片同池。

    两条约束叠加：
    * 抽帧先保底：有抽帧时至少留 layout.frame_slots 张名额（默认 1），
      避免正式图片相关度全高时抽帧永远排不上号；
    * 同一视频最多 layout.frames_per_video 张：同段片子的帧画面高度相似，
      连着放三张会显得凑数（被限额挡掉的名额，最后再回补，数量优先）。
    返回顺序仍按图片池相关度排列，首图不会被保底的抽帧抢走。
    """
    count = max(0, int(count or 0))
    if count <= 0:
        return []
    per_video = settings.get_int("layout.frames_per_video",
                                 int(getattr(config, "MEDIA_FRAME_PER_VIDEO", 2) or 0))
    slots = settings.get_int("layout.frame_slots",
                             int(getattr(config, "MEDIA_FRAME_SLOTS", 1) or 0))
    reserve = min(max(0, slots), count - 1) if count >= 2 else 0
    chosen: List[int] = []
    used = set()
    frame_used: Dict[str, int] = {}

    def take(a: Dict[str, Any]) -> bool:
        key = int(a.get("id") or 0)
        if key in used:
            return False
        if a.get("from_frame") and per_video > 0:
            src = _frame_source(a)
            if frame_used.get(src, 0) >= per_video:
                return False
            frame_used[src] = frame_used.get(src, 0) + 1
        chosen.append(key)
        used.add(key)
        return True

    for a in images:                             # 先给抽帧保底名额
        if reserve <= 0 or len(chosen) >= reserve:
            break
        if a.get("from_frame"):
            take(a)
    for a in images:                             # 其余按相关度补满
        if len(chosen) >= count:
            break
        take(a)
    if len(chosen) < count:                      # 限额挡掉的名额，最后回补
        for a in images:
            if len(chosen) >= count:
                break
            key = int(a.get("id") or 0)
            if key not in used:
                chosen.append(key)
                used.add(key)
    keep = set(chosen)
    return [a for a in images if int(a.get("id") or 0) in keep]


def pick_assets(article_id: int, max_images: int, max_videos: int) -> Tuple[List[Dict], List[Dict]]:
    """按相关度取素材；抽帧与正式图片同池竞争，并至少保底一张（见 pick_images）。"""
    images, videos = all_assets(article_id)
    return pick_images(images, max_images), videos[:max(0, max_videos)]


#: 文章长短 → (图片数, 视频数)。判据同时看字数与段落数，取更长的那个档。
_AUTO_TIERS: Tuple[Tuple[int, int, int, int], ...] = (
    # (最少字数, 最少段落数, 图片, 视频)
    (3000, 24, 6, 1),
    (1600, 14, 5, 1),
    (900, 9, 3, 1),
    (400, 5, 2, 0),
    (0, 0, 1, 0),
)


def text_metrics(blocks: List[Dict[str, str]], text: str = "") -> Dict[str, int]:
    """字数与段落数：文案短就给少素材，长文多给几张图撑版面。"""
    chars = len(re.sub(r"\s", "", text or ""))
    if not chars:
        chars = sum(len(str(b.get("text") or "")) for b in blocks)
    paras = sum(1 for b in blocks if b.get("type") == "p")
    return {"chars": chars, "paras": paras, "blocks": len(blocks)}


def plan_counts(blocks: List[Dict[str, str]], text: str = "",
                avail_images: int = 99, avail_videos: int = 99) -> Dict[str, int]:
    """按文章长短算出这次要插几张图 / 几个视频。

    * 短文（≤400 字或 ≤4 段）：1 张图，不配视频 —— 版面短，塞多了反而乱；
    * 中等（900 字 / 9 段以上）：2~3 张图，配 1 个视频；
    * 长文（1600 字 / 14 段以上）：4~5 张图，配 1 个视频；
    * 超长（3000 字 / 24 段以上）：最多 6 张图。
    自动模式受 layout.image_cap / video_cap 与素材实际数量限制；
    关掉自动模式则退回 layout.max_images / max_videos。
    """
    m = text_metrics(blocks, text)
    if settings.get_bool("layout.auto_count", True):
        images = videos = 0
        for min_chars, min_paras, img, vid in _AUTO_TIERS:
            if m["chars"] >= min_chars and m["paras"] >= min_paras:
                images, videos = img, vid
                break
        if not images:
            images, videos = 1, 0
        images = min(images, max(0, settings.get_int("layout.image_cap", 6)))
        videos = min(videos, max(0, settings.get_int("layout.video_cap", 1)))
    else:
        images = max(0, settings.get_int("layout.max_images", 2))
        videos = max(0, settings.get_int("layout.max_videos", 1))
    return {
        "images": min(images, max(0, avail_images)),
        "videos": min(videos, max(0, avail_videos)),
        "target_images": images,
        "target_videos": videos,
        "chars": m["chars"],
        "paras": m["paras"],
    }


def _poster(article_id: int, exclude: Optional[List[int]] = None) -> str:
    """视频封面：优先用抽出来的帧，尽量避开正文里已经插进去的那几张（免得同一画面出现两次）。"""
    skip = {int(x or 0) for x in (exclude or [])}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, path, url FROM media_assets WHERE article_id=? AND kind='frame'"
            " AND status='ready' ORDER BY match DESC, id ASC", (article_id,)
        ).fetchall()
    frames = [dict(r) for r in rows if _asset_file_ok(dict(r))]
    for f in frames:
        if int(f.get("id") or 0) not in skip:
            return _ref(f)
    return _ref(frames[0]) if frames else ""


# ---------------------------------------------------------------- 定插入位置
def _slot_score(blocks: List[Dict[str, str]], i: int, kind: str) -> Tuple[float, str]:
    """给「插在第 i 块之后」打分，并给出一句人话理由。"""
    score = 0.0
    prev = blocks[i - 1] if i > 0 else None
    if prev and prev["type"].startswith("h"):
        score += 3.0
        reason = f"小标题《{prev['text'][:14]}》之后"
    elif i == 0 or all(b["type"].startswith("h") for b in blocks[:i]):
        score += 2.0
        reason = "导语段之后"
    else:
        n = sum(1 for b in blocks[:i + 1] if b["type"] == "p")
        reason = f"第 {n} 段之后"
    pos = i / max(1, len(blocks) - 1)
    # 图片靠前更好看，视频靠后不打断阅读
    score += (1.0 - pos) * 1.5 if kind == "image" else pos * 1.5
    return score, reason


def suggest_placements(blocks: List[Dict[str, str]],
                       images: List[Dict], videos: List[Dict]) -> List[Dict[str, Any]]:
    """为每个素材挑一个段落边界位置，保证彼此不挨太近。"""
    gap = max(0, settings.get_int("layout.min_gap", 1))
    slots = [i for i, b in enumerate(blocks) if b["type"] == "p"]
    if not slots:
        return []
    used: List[int] = []
    out: List[Dict[str, Any]] = []
    seq = 0
    for kind, assets in (("image", images), ("video", videos)):
        for order, a in enumerate(assets):
            best: Optional[Tuple[float, int, str]] = None
            # 首图固定吃导语段：最相关的那张当头图，比被小标题抢走更自然
            if kind == "image" and order == 0 and slots[0] not in used:
                best = (0.0, slots[0], "首图：导语段之后")
            else:
                for i in slots:
                    if any(abs(i - u) <= gap for u in used):
                        continue
                    sc, reason = _slot_score(blocks, i, kind)
                    if best is None or sc > best[0]:
                        best = (sc, i, reason)
            if best is None:  # 位置不够就放宽间隔，只避开同一段
                for i in slots:
                    if i in used:
                        continue
                    sc, reason = _slot_score(blocks, i, kind)
                    if best is None or sc > best[0]:
                        best = (sc, i, reason)
            if best is None:
                continue
            seq += 1
            used.append(best[1])
            out.append({
                "id": f"m{seq}",
                "kind": kind,
                "asset_id": a["id"],
                "after_block": best[1],
                "reason": best[2],
                "enabled": 1,
            })
    out.sort(key=lambda x: (x["after_block"], 0 if x["kind"] == "image" else 1))
    return out


# ---------------------------------------------------------------- 渲染
def _ref(asset: Dict[str, Any]) -> str:
    if (settings.get_setting("layout.image_ref") or "path") == "url":
        return asset.get("url") or asset.get("path") or ""
    return asset.get("path") or asset.get("url") or ""


def _caption(asset: Dict[str, Any]) -> str:
    return str(asset.get("title") or asset.get("keyword") or "")


#: 图注字数上限：超过这个长度的「素材标题」基本都是网页标题，不是图注。
#: 取 18 也对齐头条编辑器的「图片描述（最多18字）」，短图注可以直接粘进那个框。
_CAPTION_MAX = 18


def _plain(text: str) -> str:
    """只留文字与数字，用来做「这条标题是不是页面标题」的比对。"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(text or ""))


def caption_of(asset: Dict[str, Any], title: str = "") -> str:
    """图注：素材标题优先，但把「像标题不像图注」的挡住（宁缺勿滥）。

    原文配图的 alt 常常就是网页标题（实测：首图标题 = 文章标题），
    搜索结果图的标题也多是「新台风生成,路径公布」这类新闻标题，
    直接当图注会变成图下一段莫名其妙的正文。规则：
    * 与文章标题（去标点后）相同，或长标题包含文章标题 → 视为页面标题，不给图注；
    * 结尾是感叹号 / 问号、或带两个以上逗号、或超过 20 字 → 视为标题，不给图注；
    * 其余短标题（如「台风蔷琵」）照常输出。
    """
    raw = str(asset.get("title") or asset.get("keyword") or "").strip()
    if not raw:
        return ""
    text = re.sub(r"\s+", " ", raw).strip(" -—:：|｜")
    if not text:
        return ""
    mine, page = _plain(text), _plain(title)
    if page and mine and (mine == page or (len(mine) >= 10 and mine in page)):
        return ""
    if len(text) > _CAPTION_MAX:
        return ""
    if text.endswith(("！", "!", "？", "?", "。", ".")):
        return ""
    if text.count("，") + text.count(",") >= 2 or re.search(r"[|｜]", text):
        return ""
    return text


def _media_lines(asset: Dict[str, Any], kind: str, poster: str = "",
                 title: str = "") -> List[str]:
    src = _ref(asset)
    # 视频那行只是内部备注（发布时会被丢掉），保留素材原名更好追溯
    cap = _caption(asset) if kind == "video" else caption_of(asset, title)
    if kind == "video":
        tag = f'<video src="{src}"'
        if poster:
            tag += f' poster="{poster}"'
        tag += " controls></video>"
        return [tag, "", f"> 视频：{cap}（{src}）"]
    alt = cap.replace("[", "(").replace("]", ")")
    lines = [f"![{alt}]({src})"]
    if settings.get_bool("layout.image_caption", True) and cap:
        lines += ["", f"*{cap}*"]
    return lines


def render_md(title: str, blocks: List[Dict[str, str]],
              placements: List[Dict[str, Any]], assets: List[Dict[str, Any]],
              poster: str = "", form: str = "article") -> str:
    index = {a["id"]: a for a in assets}
    by_block: Dict[int, List[Dict[str, Any]]] = {}
    for p in placements:
        if p.get("enabled"):
            by_block.setdefault(int(p["after_block"]), []).append(p)

    lines: List[str] = []
    # 短稿（微头条 / 百家动态）没有标题：正文直接从第一段开始，排版/发布都不带标题
    if title and str(form or "article") != "micro":
        lines += [f"# {title}", ""]
    for i, b in enumerate(blocks):
        if b["type"].startswith("h"):
            lines.append(f"{'#' * int(b['type'][1:])} {b['text']}")
        else:
            lines.append(b["text"])
        lines.append("")
        for p in by_block.get(i, []):
            a = index.get(p["asset_id"])
            if not a:
                continue
            lines += _media_lines(a, p["kind"], poster, title)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 主流程
def build(article_id: int, rewrite_id: Optional[int] = None,
          platform: str = "", form: str = "") -> Dict[str, Any]:
    """为一篇文章生成排版方案（覆盖该平台 · 形态的旧草稿）。

    platform/form 非空时优先用对应的改写版本 —— 头条图文、头条微头条、百家图文
    各自排版，互不覆盖；微头条不渲染标题。
    """
    req_platform = platform
    req_form = str(form or "")
    with get_conn() as conn:
        art = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        if not art:
            raise LookupError("文章不存在")
        rw = None
        if rewrite_id:
            rw = conn.execute(
                "SELECT * FROM article_rewrites WHERE id=? AND article_id=?",
                (rewrite_id, article_id)).fetchone()
        if rw is None and platform:
            sql = ("SELECT * FROM article_rewrites WHERE article_id=? AND target_platform=?"
                   " AND status='success'")
            args: List[Any] = [article_id, platform]
            if req_form:
                sql += " AND COALESCE(NULLIF(content_form,''),'article')=?"
                args.append(req_form)
            rw = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        if rw is None and platform and req_form in ("", "article"):
            # 只接受改造前的老记录（没有 target_platform），绝不跨平台借稿
            rw = conn.execute(
                "SELECT * FROM article_rewrites WHERE article_id=? AND (target_platform IS NULL"
                " OR target_platform='') AND status='success' ORDER BY id DESC LIMIT 1",
                (article_id,)).fetchone()
            if rw is None:
                raise LookupError(f"还没有{style.title_of(platform)}的改写稿，先改写该平台版本再排版")
        if rw is None and not platform:
            # 没指定平台（老入口）才允许取任意最新一条；指定了平台/形态就绝不借稿
            rw = conn.execute(
                "SELECT * FROM article_rewrites WHERE article_id=? AND status='success'"
                " ORDER BY id DESC LIMIT 1", (article_id,)).fetchone()
    if not rw:
        if platform:
            raise LookupError(f"还没有{style.label_of(platform, req_form or 'article')}"
                              f"的改写稿，先改写该版本再排版")
        raise LookupError("这篇还没有成功的改写记录，先改写再排版")

    platform = str(rw["target_platform"] or "") if "target_platform" in rw.keys() else ""
    platform = platform or req_platform
    content_form = str(rw["content_form"] or "article") if "content_form" in rw.keys() else "article"
    if req_platform and platform != req_platform:
        raise LookupError(f"改写稿属于{style.title_of(platform)}，不是请求的"
                          f"{style.title_of(req_platform)}")
    if req_form and content_form != req_form:
        raise LookupError(f"改写稿形态是{style.form_title(platform, content_form)}，"
                          f"不是请求的{style.form_title(platform, req_form)}")
    if not style.need_title(platform, content_form):
        title = ""                                # 微头条无标题
    else:
        title = rw["used_title"] or rw["origin_title"] or art["title"] or ""
    body = rw["final_text"] or rw["draft_text"] or ""
    if not body.strip():
        raise LookupError("改写正文为空，无法排版")

    blocks = parse_blocks(body)
    # 模型偶尔会把标题又抄一遍当首个小标题，去掉避免正文开头重复
    if title and blocks and blocks[0]["type"].startswith("h") and blocks[0]["text"] \
            and blocks[0]["text"] in title:
        blocks.pop(0)
    # 素材全量进快照（排版页人工增删换用），真正插入多少由文章长短决定
    all_images, all_videos = all_assets(article_id)
    counts = plan_counts(blocks, body, len(all_images), len(all_videos))
    if content_form == "micro":                   # 短稿（微头条 / 百家动态）版面短，图不贪多
        counts["images"] = min(counts["images"],
                               max(0, settings.get_int("layout.micro_image_cap", 3)))
    images = pick_images(all_images, counts["images"])
    videos = all_videos[:counts["videos"]]
    # 封面避开正文已用的抽帧，免得同一画面在文章里出现两次
    poster = _poster(article_id, [a.get("id") for a in images]) if videos else ""
    assets = all_images + all_videos
    placements = suggest_placements(blocks, images, videos)
    body_md = render_md(title, blocks, placements, assets, poster, form=content_form)

    now = _now()
    with write_conn() as conn:
        # 只清掉同平台同形态的旧草稿：人工审核结果各自保留
        conn.execute(
            "DELETE FROM article_layouts WHERE article_id=? AND platform=?"
            " AND COALESCE(NULLIF(content_form,''),'article')=? AND status='draft'",
            (article_id, platform, content_form))
        cur = conn.execute(
            """INSERT INTO article_layouts
               (article_id, rewrite_id, platform, content_form, title, body_md, blocks,
                placements, assets, image_count, video_count, status, output_path,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'draft','',?,?)""",
            (article_id, rw["id"], platform, content_form, title, body_md,
             json.dumps(blocks, ensure_ascii=False),
             json.dumps(placements, ensure_ascii=False),
             json.dumps(assets, ensure_ascii=False),
             sum(1 for p in placements if p["kind"] == "image" and p.get("enabled")),
             sum(1 for p in placements if p["kind"] == "video" and p.get("enabled")),
             now, now))
        layout_id = int(cur.lastrowid or 0)
    return detail(layout_id) or {}


def compose_body(d: Dict[str, Any], blocks: List[Dict[str, str]],
                 placements: List[Dict[str, Any]], assets: List[Dict[str, Any]],
                 title: str) -> str:
    """按 blocks / placements / assets 重渲染正文。

    详情、复制、发布、导出共用这一份口径：素材换过、图注规则改过之后，
    老排版记录里的 body_md 不该继续拿旧快照糊弄人。
    """
    has_video = any(p.get("kind") == "video" for p in placements)
    poster = (_poster(int(d.get("article_id") or 0),
                      [p.get("asset_id") for p in placements if p.get("kind") == "image"])
              if has_video else "")
    return render_md(title, blocks, placements, assets, poster,
                     form=str(d.get("content_form") or "article"))


def detail(layout_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM article_layouts WHERE id=?", (layout_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    # 老数据没有 content_form：按图文处理
    d["content_form"] = str(d.get("content_form") or "article")
    d["need_title"] = style.need_title(str(d.get("platform") or ""), d["content_form"])
    for key in ("blocks", "placements", "assets"):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except ValueError:
            d[key] = []
    # 挑了但没排上位置的素材（通常是正文段落太少），页面上要能看见
    used_ids = {p.get("asset_id") for p in d["placements"] if p.get("enabled")}
    d["unused"] = [a for a in d["assets"] if a.get("id") not in used_ids]
    n_img = sum(1 for a in d["assets"] if a.get("kind") == "image")
    n_vid = sum(1 for a in d["assets"] if a.get("kind") == "video")
    plan = plan_counts(d["blocks"], "", n_img, n_vid)
    if d["content_form"] == "micro":              # 短稿（微头条 / 百家动态）图不贪多
        plan["images"] = min(plan["images"],
                             max(0, settings.get_int("layout.micro_image_cap", 3)))
    d["plan"] = plan
    # 正文按当前素材重渲染（老记录的 body_md 可能是旧图注 / 已换掉的素材）
    fresh = compose_body(d, d["blocks"], d["placements"], d["assets"],
                         str(d.get("title") or ""))
    if fresh.strip():
        d["body_md"] = fresh
    # 一键复制的默认参数：前端下拉不用再多打一次接口，跟着排版记录一起返回
    d["copy"] = {
        "mode": settings.get_setting("layout.copy_image_mode")
                or getattr(config, "COPY_MODE", "inline"),
        "max_width": settings.get_int("layout.copy_image_max_width",
                                      int(getattr(config, "COPY_MAX_WIDTH", 1080))),
        "quality": settings.get_int("layout.copy_image_quality",
                                    int(getattr(config, "COPY_JPEG_QUALITY", 82))),
    }
    # 敏感词状态：排版页要先看见「为什么交付不了」，才知道该改哪儿
    d["sensitive"] = rewrite_sensitive(d.get("rewrite_id"))
    return d


def for_platform(article_id: int, platform: str,
                 form: str = "") -> Optional[Dict[str, Any]]:
    """严格取某个「平台 · 形态」的排版稿；没有就按对应改写稿现排一份。

    「严格」很关键：发布时如果拿不到目标版本的排版稿，宁可让任务失败并提示，
    也不能把另一平台/另一形态的稿子发过去。
    """
    form = str(form or "")
    with get_conn() as conn:
        sql = "SELECT id FROM article_layouts WHERE article_id=? AND platform=?"
        args: List[Any] = [article_id, platform]
        if form:
            sql += " AND COALESCE(NULLIF(content_form,''),'article')=?"
            args.append(form)
        row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        if row is None:
            # 老记录没有 platform 列值：用改写记录的 target_platform 反查
            sql = ("SELECT l.id FROM article_layouts l"
                   " JOIN article_rewrites r ON r.id = l.rewrite_id"
                   " WHERE l.article_id=? AND r.target_platform=?")
            args = [article_id, platform]
            if form:
                sql += " AND COALESCE(NULLIF(r.content_form,''),'article')=?"
                args.append(form)
            row = conn.execute(sql + " ORDER BY l.id DESC LIMIT 1", args).fetchone()
    if row:
        return detail(row["id"])
    try:
        return build(article_id, platform=platform, form=form) or None
    except LookupError:
        return None


def latest(article_id: int, platform: str = "",
           form: str = "") -> Optional[Dict[str, Any]]:
    """最近一条排版。

    传 platform（可再传 form）时只认该版本（含老记录的 target_platform 反查），
    取不到返回 None —— 让调用方去重建，绝不把另一版本的排版冒充过来。
    """
    form = str(form or "")
    with get_conn() as conn:
        row = None
        if platform:
            sql = "SELECT id FROM article_layouts WHERE article_id=? AND platform=?"
            args: List[Any] = [article_id, platform]
            if form:
                sql += " AND COALESCE(NULLIF(content_form,''),'article')=?"
                args.append(form)
            row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
            if row is None:
                sql = ("SELECT l.id FROM article_layouts l"
                       " JOIN article_rewrites r ON r.id = l.rewrite_id"
                       " WHERE l.article_id=? AND r.target_platform=?")
                args = [article_id, platform]
                if form:
                    sql += " AND COALESCE(NULLIF(r.content_form,''),'article')=?"
                    args.append(form)
                row = conn.execute(sql + " ORDER BY l.id DESC LIMIT 1", args).fetchone()
            if row is None:
                return None
        if row is None:
            row = conn.execute(
                "SELECT id FROM article_layouts WHERE article_id=? ORDER BY id DESC LIMIT 1",
                (article_id,)).fetchone()
    return detail(row["id"]) if row else None


def refresh_assets(layout_id: int) -> Optional[Dict[str, Any]]:
    """素材重采后再点排版时，把快照换成最新的一批（保留已插入的素材位置）。"""
    d = detail(layout_id)
    if not d:
        return None
    images, videos = all_assets(int(d["article_id"] or 0))
    assets = images + videos
    valid = {a["id"]: a for a in assets}
    placements = [p for p in d["placements"] if int(p.get("asset_id") or 0) in valid]
    with write_conn() as conn:
        conn.execute(
            "UPDATE article_layouts SET assets=?, placements=?, updated_at=? WHERE id=?",
            (json.dumps(assets, ensure_ascii=False),
             json.dumps(placements, ensure_ascii=False), _now(), layout_id))
    return detail(layout_id)


def rewrite_sensitive(rewrite_id: Any) -> Dict[str, Any]:
    """这条排版稿对应的改写稿当前的敏感词状态。

    排版页要让人一眼看出「为什么这篇交付不了」：命中哪些词、是否已人工放行。
    """
    rid = int(rewrite_id or 0)
    if not rid:
        return {"status": "", "words": [], "waived": False, "note": ""}
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sensitive_status, sensitive_hits, sensitive_waived_note"
            " FROM article_rewrites WHERE id=?", (rid,)).fetchone()
    if not row:
        return {"status": "", "words": [], "waived": False, "note": ""}
    try:
        hits = json.loads(row["sensitive_hits"] or "[]")
    except (TypeError, ValueError):
        hits = []
    words = [str(h.get("word")) for h in hits if isinstance(h, dict) and h.get("word")]
    status = str(row["sensitive_status"] or "")
    return {"status": status, "words": words[:12], "waived": status == "waived",
            "note": str(row["sensitive_waived_note"] or "")}


def sync_rewrite_text(d: Dict[str, Any], blocks: List[Dict[str, str]],
                      title: str) -> Optional[Dict[str, Any]]:
    """人工改过的正文写回改写稿，并重跑敏感词校验。

    交付闸门（finish._export_platform）查的是改写稿的 final_text，
    自动发布读的也是排版稿渲染结果，所以两处必须一致：
    改完正文不同步回去，就会出现「页面上改干净了、交付却被旧文本拦下」。
    """
    rid = int(d.get("rewrite_id") or 0)
    if not rid or not blocks:
        return None
    form = str(d.get("content_form") or "article")
    platform = str(d.get("platform") or "")
    body = blocks_to_text(blocks)
    plain = "\n".join(b["text"] for b in blocks)
    report = sensitive.check(plain, title if style.need_title(platform, form) else "")
    status = "pass" if report.get("ok") else "blocked"
    now = _now()
    with get_conn() as conn:
        old = conn.execute("SELECT final_text FROM article_rewrites WHERE id=?",
                           (rid,)).fetchone()
    before = str(old["final_text"] or "") if old else ""
    with write_conn() as conn:
        conn.execute(
            "UPDATE article_rewrites SET final_text=?, words=?, sensitive_status=?,"
            " sensitive_hits=?, sensitive_checked_at=? WHERE id=?",
            (body, len(re.sub(r"\s", "", plain)), status,
             json.dumps(report.get("hits") or [], ensure_ascii=False), now, rid))
        # 留痕：round=99 表示「排版页手工改稿」（0 是润色后首检，1..N 是自动修复轮次）
        conn.execute(
            "INSERT INTO sensitive_checks (article_id, rewrite_id, round, platform,"
            " content_form, status, hits, before_text, after_text, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(d.get("article_id") or 0), rid, 99, platform, form,
             "pass" if report.get("ok") else "fail",
             json.dumps(report.get("hits") or [], ensure_ascii=False),
             before, body, now))
    return dict(report, status=status, synced=1, rewrite_id=rid,
                _layout_id=int(d.get("id") or 0))


def update(layout_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
    """手动调整：改正文 / 开关插入点 / 换素材 / 增删素材 / 挪位置，改完重渲染。"""
    d = detail(layout_id)
    if not d:
        return None
    blocks = d["blocks"]
    blocks_changed = False
    if isinstance(fields.get("blocks"), (list, tuple)):
        blocks = clean_blocks(fields.pop("blocks"))
        if not blocks:
            raise LookupError("正文不能为空，至少留一段")
        blocks_changed = blocks != d["blocks"]
    placements = d["placements"]
    if "placements" in fields and isinstance(fields["placements"], list):
        placements = fields.pop("placements")
    title = str(fields.pop("title", d.get("title") or ""))
    for p in placements:
        p["enabled"] = 1 if p.get("enabled") else 0
        if int(p.get("after_block") or 0) >= len(blocks):
            p["after_block"] = max(0, len(blocks) - 1)
    assets = d["assets"]
    # 允许引用快照里没有的新素材（例如刚重采的图）：补进快照再渲染
    known = {a.get("id") for a in assets}
    missing = [int(p.get("asset_id") or 0) for p in placements
               if int(p.get("asset_id") or 0) and int(p.get("asset_id") or 0) not in known]
    if missing:
        extra_imgs, extra_vids = all_assets(int(d["article_id"] or 0))
        for a in extra_imgs + extra_vids:
            if a["id"] in missing and a["id"] not in known:
                assets.append(a)
                known.add(a["id"])
    form = str(d.get("content_form") or "article")
    if not style.need_title(str(d.get("platform") or ""), form):
        title = ""                                 # 微头条不能有标题
    body_md = compose_body(d, blocks, placements, assets, title)

    sets = ["updated_at=?"]
    args: List[Any] = [_now()]
    if blocks_changed and "status" not in fields:
        fields["status"] = "draft"                 # 正文改过：之前的导出/交付算过期
    for col in ("status", "output_path"):
        if col in fields:
            sets.append(f"{col}=?")
            args.append(fields.pop(col))
    args += [layout_id]
    with write_conn() as conn:
        conn.execute(
            f"""UPDATE article_layouts SET title=?, blocks=?, body_md=?, placements=?,
                assets=?, image_count=?, video_count=?, {', '.join(sets)} WHERE id=?""",
            (title, json.dumps(blocks, ensure_ascii=False), body_md,
             json.dumps(placements, ensure_ascii=False),
             json.dumps(assets, ensure_ascii=False),
             sum(1 for p in placements if p["kind"] == "image" and p.get("enabled")),
             sum(1 for p in placements if p["kind"] == "video" and p.get("enabled")),
             *args))
    # 先同步改写稿再读详情：返回体里的 sensitive 摘要必须是这次改稿后的结论
    report = sync_rewrite_text(dict(d, id=layout_id), blocks, title) if blocks_changed else None
    item = detail(layout_id)
    if item is not None and report:
        item["sensitive_edit"] = report
    return item


def delete(layout_id: int) -> bool:
    with write_conn() as conn:
        cur = conn.execute("DELETE FROM article_layouts WHERE id=?", (layout_id,))
    return bool(cur.rowcount)


def export(layout_id: int) -> Dict[str, Any]:
    """把排版稿写到 output/<标题>/排版稿.md，同时存一份 JSON 便于程序取用。"""
    d = detail(layout_id)
    if not d:
        raise LookupError("排版记录不存在")
    with get_conn() as conn:
        art = conn.execute("SELECT * FROM articles WHERE id=?", (d["article_id"],)).fetchone()
    if not art:
        raise LookupError("文章不存在")
    directory = exporter.article_dir(dict(art))
    directory.mkdir(parents=True, exist_ok=True)
    md_path = directory / "排版稿.md"
    md_path.write_text(d["body_md"] or "", encoding="utf-8")
    json_path = directory / "排版.json"
    json_path.write_text(json.dumps({
        "article_id": d["article_id"],
        "rewrite_id": d["rewrite_id"],
        "title": d["title"],
        "body_md": d["body_md"],
        "placements": d["placements"],
        "assets": d["assets"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    rel = str(md_path.relative_to(exporter.OUTPUT_DIR)).replace("\\", "/")
    update(layout_id, status="exported", output_path=rel)
    return {"path": rel, "url": exporter.output_url(rel), "dir": str(directory)}
