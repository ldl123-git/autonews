"""复制成稿：把排版稿渲染成能直接粘进平台编辑器的富文本。

手动发布的现实约束（都在驱动代码里确认过）：
* 头条正文只认自家图床地址（见 toutiao.py 的 IMG_WHITE / 7115），所以「粘贴带图」
  只有两条路：让编辑器在粘贴时自己上传（inline 模式，图片 data:base64 内联，不触网），
  或者先把图传到平台图床再复制（upload 模式，最稳，但要账号登录态、慢一些）。
* 视频没法通过剪贴板带过去：用该视频的抽帧图占位，并注明原片文件名。
* 排版稿的 body_md 是给本地预览 / 出版用的（图片是本地绝对路径、图注带 * *），
  不能直接拿去粘贴，因此这里按 blocks / placements 重新渲染「粘贴专用 HTML」：
  一段一个 <p>（编辑器的软换行语义各不相同，<br/> 常被合并成一段），
  小标题降为 <h2>/<h3>，图注用不带 class 的 <p>（编辑器普遍会剥 class）。

对外只暴露 build_copy()：给定排版记录，返回 {title, html, text, stats, videos, warnings}。
"""
import base64
import html as html_lib
import io
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import accounts, config, settings
from .. import layout as layout_mod
from ..layout import detail as layout_detail

log = logging.getLogger(__name__)

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MODES = ("inline", "upload", "text")
_MODE_LABEL = {"inline": "内联图片", "upload": "平台图床", "text": "纯文字"}


# ---------------------------------------------------------------- 文本渲染
def _esc(text: Any) -> str:
    """HTML 转义（含引号）：正文与属性值都用它，避免素材标题里的 < > 破版面。"""
    return html_lib.escape(str(text or ""), quote=True)


def _rich(text: Any) -> str:
    """转义 + 保留 **加粗**。"""
    return _BOLD.sub(r"<strong>\1</strong>", _esc(text))


def _caption_of(asset: Dict[str, Any]) -> str:
    return str(asset.get("title") or asset.get("keyword") or "")


def _file_name(asset: Dict[str, Any]) -> str:
    return str(asset.get("name") or Path(str(asset.get("path") or "")).name or "")


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _asset_id(asset: Dict[str, Any]) -> int:
    return _as_int(asset.get("id"))


# ---------------------------------------------------------------- 图片编码
def _encode_jpeg(path: Path, max_width: int, quality: int) -> bytes:
    """读图 → 等比缩到 max_width → JPEG 编码（不落盘，直接给剪贴板）。"""
    from PIL import Image

    with Image.open(path) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if max_width and w > max_width:
            new_h = max(1, int(round(h * max_width / float(w))))
            img = img.resize((max_width, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=max(30, min(95, int(quality))), optimize=True)
    return buf.getvalue()


def _inline_src(path: Path, max_width: int, quality: int,
                per_image: int, budget: Dict[str, int],
                warnings: List[str]) -> str:
    """把一张本地图压成 data URI。

    单张先按 COPY_MAX_IMAGE_KB 降质（quality -12 / 宽度 0.8×、0.6×），
    整篇再按 COPY_MAX_TOTAL_MB 守门：超了继续降质并留一条提示，不阻断复制。
    """
    steps = [
        (max_width, quality),
        (max_width, max(40, quality - 12)),
        (int(max_width * 0.8) or 800, max(38, quality - 22)),
        (int(max_width * 0.6) or 640, 55),
    ]
    data = b""
    for width, q in steps:
        try:
            data = _encode_jpeg(path, width, q)
        except ImportError:
            warnings.append("缺少 Pillow，已按原图字节内联（体积可能较大）")
            return _raw_src(path)
        except Exception as exc:  # noqa: BLE001 - 单张失败不该毁掉整次复制
            warnings.append(f"图片压缩失败：{path.name}（{exc}）")
            return ""
        if len(data) <= per_image or width <= 640:
            break

    if budget.get("total", 0) + len(data) > budget.get("limit", 0) > 0:
        # 总量超了：再压一档，仍超就照旧带走（宁可慢一点，也不能缺图）
        try:
            smaller = _encode_jpeg(path, max(640, int(max_width * 0.6)), 52)
            if len(smaller) < len(data):
                data = smaller
                warnings.append("图片较多，已自动压缩到更小体积")
        except Exception:  # noqa: BLE001
            pass
    budget["total"] = budget.get("total", 0) + len(data)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def _raw_src(path: Path) -> str:
    """Pillow 不可用时的兜底：原字节内联，按后缀猜 MIME。"""
    try:
        blob = path.read_bytes()
    except OSError:
        return ""
    suffix = path.suffix.lower()
    mime = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(
        suffix, "image/jpeg")
    return f"data:{mime};base64," + base64.b64encode(blob).decode()


# ---------------------------------------------------------------- 素材顺序
def _sequence(detail: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """把排版稿摊成「正文块 → 素材 → 正文块 …」的线性序列。

    渲染顺序必须和排版页看到的完全一致（正文里插在哪一段之后），
    所以这里照 placements.after_block 挂回块后面，而不是按素材池顺序。
    """
    blocks = detail.get("blocks") or []
    assets = list(detail.get("assets") or [])
    index = {_asset_id(a): a for a in assets}
    placements = [p for p in (detail.get("placements") or []) if p.get("enabled")]
    by_block: Dict[int, List[Dict[str, Any]]] = {}
    for p in placements:
        try:
            pos = int(p.get("after_block") or 0)
        except (TypeError, ValueError):
            pos = 0
        by_block.setdefault(pos, []).append(p)

    seq: List[Tuple[str, Dict[str, Any]]] = []
    for i, b in enumerate(blocks):
        seq.append(("block", b))
        for p in by_block.get(i, []):
            # 注意取的是 asset_id（placement 自己的 id 是 "m1" 这种字符串）
            a = index.get(_as_int(p.get("asset_id")))
            if not a:
                continue
            kind = "video" if p.get("kind") == "video" else "image"
            page_title = str(detail.get("title") or "")
            item: Dict[str, Any] = {
                "kind": kind, "asset": a,
                # 图注要像图注：页面标题 / 新闻标题这类素材标题会被挡掉（见 layout.caption_of）
                "caption": (_caption_of(a) if kind == "video"
                            else layout_mod.caption_of(a, page_title)),
                "file": _file_name(a),
            }
            if kind == "video":
                frames = layout_mod.video_frames(assets, a)
                item["frame"] = frames[0] if frames else None
                item["has_frame"] = bool(frames)
            seq.append(("media", item))
    return seq


# ---------------------------------------------------------------- 平台图床
def _pick_account(platform: str, account_id: Optional[int]) -> Optional[Dict[str, Any]]:
    if account_id:
        acc = accounts.get_account(int(account_id), include_cookie=True)
        if acc:
            return acc
    if not platform:
        return None
    for acc in accounts.list_accounts(platform, include_cookie=True):
        if acc.get("enabled"):
            return acc
    return None


def _upload_images(paths: List[str], platform: str, account_id: Optional[int],
                   warnings: List[str]) -> Dict[int, str]:
    """把本地图片传到平台图床，返回 {paths 下标: 平台 URL}。

    失败的图不返回，调用方会退回内联 —— 少一张图比整次复制失败更糟。
    """
    acc = _pick_account(platform, account_id)
    if not acc:
        title = accounts.platform_title(platform) if platform else "当前平台"
        warnings.append(f"没有启用的{title}账号，图片已自动改回内联模式")
        return {}
    from . import get_driver

    driver = get_driver(acc)
    if driver is None:
        warnings.append(f"{acc.get('platform')} 还没有分发驱动，图片已改回内联模式")
        return {}
    try:
        urls = driver.upload_images([str(p) for p in paths])
    except Exception as exc:  # noqa: BLE001 - 传图失败就整体回退内联
        log.warning("复制稿传图失败：%s", exc)
        warnings.append(f"传图到平台图床失败，图片已改回内联模式：{exc}")
        return {}
    out: Dict[int, str] = {}
    failed = 0
    for i, url in enumerate(urls or []):
        if url:
            out[i] = str(url)
        else:
            failed += 1
    if failed:
        warnings.append(f"{failed} 张图没传上平台图床，已改回内联")
    return out


# ---------------------------------------------------------------- 组装
def _render(seq: List[Tuple[str, Dict[str, Any]]], *, src_of, caption_on: bool,
            mode: str, warnings: List[str]) -> Tuple[str, str, Dict[str, int]]:
    html_parts: List[str] = []
    text_parts: List[str] = []
    stats = {"images": 0, "video_slots": 0, "captions": 0, "inline_bytes": 0}

    for tag, item in seq:
        if tag == "block":
            raw = str(item.get("text") or "")
            if not raw.strip():
                continue
            typ = str(item.get("type") or "p")
            if typ == "h1":            # 标题单独进平台标题框，正文不重复
                continue
            if typ.startswith("h"):
                level = max(2, min(4, int(typ[1:])))
                html_parts.append(f"<h{level}>{_rich(raw)}</h{level}>")
            else:
                html_parts.append(f"<p>{_rich(raw)}</p>")
            text_parts.append(_BOLD.sub(r"\1", raw))   # 纯文本版不留 ** 标记
            continue

        cap = str(item.get("caption") or "")
        if item.get("kind") == "video":
            stats["video_slots"] += 1
            frame = item.get("frame")
            src = src_of(frame) if frame else ""
            if src:
                html_parts.append(f'<img src="{src}" alt="{_esc(cap)}" />')
                stats["images"] += 1
            note = f"【视频位】{cap}（原片 {item.get('file') or '见交付目录 素材/视频'}，" \
                   f"请在此手动上传视频）"
            html_parts.append(f"<p>{_esc(note)}</p>")
            text_parts.append(note)
            continue

        src = src_of(item.get("asset")) if mode != "text" else ""
        if not src:
            if mode == "text":
                note = f"〔配图：{cap or '无图注'}{('｜' + item['file']) if item.get('file') else ''}〕"
                html_parts.append(f"<p>{_esc(note)}</p>")
                text_parts.append(note)
            else:
                warnings.append(f"图片文件缺失，已跳过：{item.get('file') or cap}")
            continue
        stats["images"] += 1
        html_parts.append(f'<img src="{src}" alt="{_esc(cap)}" />')
        if caption_on and cap:
            stats["captions"] += 1
            html_parts.append(f"<p>{_esc(cap)}</p>")
            text_parts.append(cap)

    body = "\n".join(html_parts)
    plain = re.sub(r"\n{3,}", "\n\n", "\n\n".join(text_parts)).strip()
    return body, (plain + "\n") if plain else "", stats


def _dedup(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for s in items:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---------------------------------------------------------------- 对外入口
def build_copy(layout_id: int, mode: str = "", platform: str = "",
               account_id: Optional[int] = None, max_width: int = 0,
               quality: int = 0) -> Dict[str, Any]:
    """把一条排版记录做成「可直接粘进平台编辑器」的富文本载荷。

    mode：
    * inline（默认）：图片压成 data:base64 内联，粘贴后由平台编辑器自己上传；
    * upload：先经分发驱动传到平台图床，正文里写平台地址（失败自动回退内联）；
    * text：只给纯文本，图片位置留一行占位。
    """
    d = layout_detail(int(layout_id))
    if not d:
        raise LookupError("排版记录不存在")

    mode = str(mode or settings.get_setting("layout.copy_image_mode")
               or getattr(config, "COPY_MODE", "inline")).strip().lower()
    if mode not in _MODES:
        mode = "inline"
    width = int(max_width or settings.get_int("layout.copy_image_max_width",
                                              int(getattr(config, "COPY_MAX_WIDTH", 1080))) or 0)
    q = int(quality or settings.get_int("layout.copy_image_quality",
                                        int(getattr(config, "COPY_JPEG_QUALITY", 82))) or 0)
    width = max(480, min(2400, width))
    q = max(40, min(95, q))

    form = str(d.get("content_form") or "article")
    title = "" if form == "micro" else str(d.get("title") or "")
    platform = str(platform or d.get("platform") or "")
    seq = _sequence(d)
    warnings: List[str] = []

    per_image = int(getattr(config, "COPY_MAX_IMAGE_KB", 600)) * 1024
    budget = {"total": 0,
              "limit": int(getattr(config, "COPY_MAX_TOTAL_MB", 8)) * 1024 * 1024}

    # 平台图床模式：先把「要出现在正文里的图片」按顺序传一遍（同一视频的抽帧也算一张）
    uploaded: Dict[int, str] = {}
    if mode == "upload":
        paths: List[str] = []
        keys: List[int] = []
        seen: set = set()
        limit = int(getattr(config, "COPY_UPLOAD_LIMIT", 10) or 0)
        for tag, item in seq:
            if tag != "media":
                continue
            asset = item.get("frame") if item.get("kind") == "video" else item.get("asset")
            if not asset:
                continue
            aid = _asset_id(asset)
            if aid and aid in seen:      # 同一张图在正文里出现两次，传一次就够
                continue
            path = str(asset.get("path") or "")
            if not path or not Path(path).exists():
                continue
            if limit and len(paths) >= limit:
                warnings.append(f"图片超过 {limit} 张，其余改回内联上传")
                break
            paths.append(path)
            keys.append(aid)
            if aid:
                seen.add(aid)
        if paths:
            got = _upload_images(paths, platform, account_id, warnings)
            for i, aid in enumerate(keys):
                url = got.get(i)
                if url:
                    uploaded[aid] = url

    cache: Dict[int, str] = {}

    def src_of(asset: Optional[Dict[str, Any]]) -> str:
        if not asset:
            return ""
        aid = _asset_id(asset)
        if aid and aid in uploaded:
            return uploaded[aid]
        if aid and aid in cache:
            return cache[aid]
        path = Path(str(asset.get("path") or ""))
        if not str(asset.get("path") or "") or not path.exists():
            cache[aid] = ""
            return ""
        src = _inline_src(path, width, q, per_image, budget, warnings)
        cache[aid] = src
        return src

    caption_on = settings.get_bool("layout.image_caption", True)
    body, plain, stats = _render(seq, src_of=src_of, caption_on=caption_on,
                                 mode=mode, warnings=warnings)
    stats["inline_bytes"] = int(budget.get("total", 0))
    stats["uploaded"] = len(uploaded)

    videos = [{"title": it.get("caption") or "", "file": it.get("file") or "",
               "placeholder": bool(it.get("has_frame"))}
              for tag, it in seq if tag == "media" and it.get("kind") == "video"]

    return {
        "layout_id": int(layout_id),
        "article_id": d.get("article_id"),
        "title": title,
        "form": form,
        "mode": mode,
        "mode_label": _MODE_LABEL.get(mode, mode),
        "platform": platform,
        "html": body,
        "text": plain,
        "stats": dict(stats, chars=len(re.sub(r"\s", "", plain)),
                      videos=len(videos)),
        "videos": videos,
        "warnings": _dedup(warnings),
    }
