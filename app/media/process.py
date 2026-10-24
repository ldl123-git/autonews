"""素材二次加工：

图片 —— 裁边 → 居中裁剪到目标比例 → 裁掉边缘文字条（水印 / 台标 / 字幕）→ 缩放
        → 格式转换 → 压缩 → 隐私处理（清除 EXIF / GPS / 拍摄信息）
        ※ 产出图片上不叠加任何文字：IMG_WATERMARK 保持为空，只有显式传参才会加水印。
视频 —— 探测时长跳过片头截取 10~15 秒 → 采样多帧检测文字条并 crop 掉
        → 转码 H.264 → 缩放压缩 → 可选去音轨 → 均匀抽帧
        → 抽出的帧按图片标准再加工（统一尺寸 / 去 EXIF / 去字样）
"""
import io
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config
from ..settings import resolve_binary


def ffmpeg_bin() -> str:
    """ffmpeg 路径：优先「系统设置」页配置，其次系统 PATH。"""
    return resolve_binary("media.ffmpeg", "ffmpeg")


def ffprobe_bin() -> str:
    """ffprobe 路径：优先「系统设置」页配置，其次系统 PATH。"""
    return resolve_binary("media.ffprobe", "ffprobe")


class ProcessError(RuntimeError):
    pass


# ---------------------------------------------------------------- 图片
def _require_pillow():
    try:
        from PIL import Image  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise ProcessError("缺少 Pillow，请先执行：pip install Pillow") from exc


def image_size(data: bytes) -> tuple:
    """读取图片原始宽高（不落盘），用于过滤图标 / 缩略图 / 装饰图。

    无法判定时返回 (-1, -1)，调用方据此跳过尺寸校验（例如没装 Pillow 的环境）。
    """
    try:
        _require_pillow()
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:  # noqa: BLE001
        return (-1, -1)


def is_text_plate(data: bytes) -> bool:
    """判定是否为「整张都是字」的图：警方通报截图 / 聊天记录 / 公告文书。

    这类图不符合「图片上不带字样」的产出要求，应作为素材拒收。
    判据：画面被单一底色大面积覆盖（截图特征）+ 边缘能量高（满屏文字）。
    """
    try:
        from PIL import Image, ImageFilter
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return False
    img.thumbnail((200, 200))
    w, h = img.size
    if w < 32 or h < 32:
        return False
    hist = img.convert("L").filter(ImageFilter.FIND_EDGES).histogram()
    total = sum(hist) or 1
    energy = sum(i * n for i, n in enumerate(hist)) / total
    colors = sorted(img.quantize(colors=8).getcolors(w * h) or [], reverse=True)
    top = colors[0][0] / float(w * h) if colors else 0.0
    # 实测定标：真实照片 top≤0.3、energy≥18；通报截图 top≥0.8、energy≈12
    return top >= 0.55 and energy >= 9


def is_logo_banner(data: bytes) -> bool:
    """判定台标 / 媒体 logo / 栏目横幅（例如「CBH 中国商报」那种红底白字台标）。

    这类图往往来自原文页的表头或页脚，与新闻内容无关，插进正文很像广告。
    判据（偏保守，宁可漏判也不错杀真实照片）：
    1. 超宽横幅（宽高比 ≥ 3）；
    2. 画面基本由 1~2 种颜色构成且占据 ≥ 85%（合成图特征，真实照片量化后色块很散）。
    真正的新闻配图即便有大面积天空 / 雪地，量化到 16 色后仍会占满多个色块。
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return False
    w, h = img.size
    if w < 32 or h < 32:
        return True
    if w / float(h) >= 3.0:
        return True
    img.thumbnail((200, 200))
    w, h = img.size
    total = w * h or 1
    bins = sorted(img.quantize(colors=16).getcolors(total) or [], reverse=True)
    if not bins:
        return False
    used = [n for n, _ in bins if n / float(total) >= 0.005]
    top2 = sum(n for n, _ in bins[:2]) / float(total)
    return len(used) <= 4 and top2 >= 0.85


def image_fingerprint(data: bytes) -> str:
    """图片内容指纹（dHash，16 位十六进制）：同一幅画面不同尺寸 / 压缩的哈希基本不变。

    用于剔除重复素材 —— 同一张图在多个站点往往有多个 URL，字节不同但内容一样。
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("L").resize((9, 8), Image.LANCZOS)
    except Exception:  # noqa: BLE001
        return ""
    px = list(img.getdata())
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return f"{bits:016x}"


def fingerprint_distance(a: str, b: str) -> int:
    """两个指纹的汉明距离（不同位数），为空返回 999。"""
    if not a or not b or len(a) != len(b):
        return 999
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def duplicate_of(fingerprint: str, known: List[str], max_dist: int = 0) -> bool:
    """判定是否与已有素材重复（汉明距离 ≤ max_dist 视为同一张图）。

    纯色 / 几乎无细节的图 dHash 会退化成全 0 或全 1（两张完全不同的纯色图也会撞），
    这种指纹不参与去重判定，避免误杀。
    """
    if not fingerprint or not known:
        return False
    value = int(fingerprint, 16)
    if value == 0 or value == (1 << (len(fingerprint) * 4)) - 1:
        return False
    limit = max_dist or getattr(config, "MEDIA_DEDUP_MAX_DIST", 6)
    return any(fingerprint_distance(fingerprint, k) <= limit for k in known if k)


def center_crop(img, aspect: float):
    """按目标宽高比居中裁剪。"""
    w, h = img.size
    target = aspect
    if abs(w / h - target) < 0.01:
        return img
    if w / h > target:      # 太宽，裁左右
        new_w = int(round(h * target))
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    new_h = int(round(w / target))   # 太高，裁上下
    top = (h - new_h) // 2
    return img.crop((0, top, w, top + new_h))


def trim_border(img, ratio: float):
    """四周裁掉固定比例（贴边水印 / 黑边的通用解法）。"""
    ratio = float(ratio or 0)
    if ratio <= 0 or ratio >= 0.4:
        return img
    w, h = img.size
    dx, dy = int(w * ratio), int(h * ratio)
    if dx < 2 or dy < 2:
        return img
    return img.crop((dx, dy, w - dx, h - dy))


def _band_metrics(img, box: Tuple[int, int, int, int]):
    """条带特征：边缘能量 / 强边缘像素占比 / 平均饱和度。"""
    from PIL import ImageFilter
    band = img.crop(box)
    w, h = band.size
    if w < 16 or h < 16:
        return None
    gray = band.convert("L")
    hist = gray.filter(ImageFilter.FIND_EDGES).histogram()
    total = sum(hist) or 1
    energy = sum(i * n for i, n in enumerate(hist)) / total
    strong = sum(hist[64:]) / total
    small = band.convert("RGB")
    small.thumbnail((64, 64))
    px = list(small.getdata())
    if not px:
        return None
    sat = sum(max(p) - min(p) for p in px) / len(px)
    return energy, strong, sat


def _crop_out_band(size: Tuple[int, int], box: Tuple[int, int, int, int]):
    """返回「去掉 box 之后」的裁剪框。"""
    w, h = size
    x0, y0, x1, y1 = box
    if y0 <= 0 and y1 >= h:                 # 竖向条带（左 / 右）
        return (x1, 0, w, h) if x0 <= 0 else (0, 0, x0, h)
    return (0, y1, w, h) if y0 <= 0 else (0, 0, w, y0)


def _band_box(size: Tuple[int, int], name: str, ratio: float) -> Tuple[int, int, int, int]:
    """边名 + 比例 → 条带矩形。"""
    w, h = size
    n = int((w if name in ("left", "right") else h) * ratio)
    if name == "bottom":
        return (0, h - n, w, h)
    if name == "top":
        return (0, 0, w, n)
    if name == "right":
        return (w - n, 0, w, h)
    return (0, 0, n, h)


def detect_text_band(img):
    """在四边 × 10% / 18% / 26% 三档高度里找最像文字条的一条边。

    判据保守，宁可漏裁也不错裁：
    1. 条带的边缘能量显著高于画面主体（文字 = 密集高频边缘）；
    2. 强边缘像素占比落在「文字」区间：既不是纯色块，也不是满屏噪点；
    3. 条带饱和度不能太高（纯色块 / 大面积彩色装饰不算文字）。

    返回 (边名, 比例) 或 None。
    """
    w, h = img.size
    whole = _band_metrics(img, (0, 0, w, h))
    if not whole:
        return None
    base = max(whole[0], 1.0)
    best = None
    for name in ("bottom", "top", "right", "left"):
        for ratio in (0.10, 0.18, 0.26):
            m = _band_metrics(img, _band_box(img.size, name, ratio))
            if not m:
                continue
            energy, strong, sat = m
            if energy < 14 or energy < 2.0 * base:
                continue
            if not (0.02 <= strong <= 0.50):
                continue
            if sat > 80:                 # 大面积彩色装饰不是文字条
                continue
            # 边缘能量越突出、条带越靠外圈越优先
            score = (energy / base) * (1.0 + ratio)
            if best is None or score > best[0]:
                best = (score, name, ratio)
    if not best:
        return None
    return best[1], best[2]


def strip_text_bands(img, max_rounds: int = 2):
    """裁掉压在边缘的文字条（站名水印 / 电视台标 / 字幕 / 通栏标题条）。

    返回 (新图, [被裁掉的边名])
    """
    bands: List[str] = []
    for _ in range(max_rounds):
        hit = detect_text_band(img)
        if not hit or hit[0] in bands:
            break
        name, ratio = hit
        img = img.crop(_crop_out_band(img.size, _band_box(img.size, name, ratio)))
        bands.append(name)
        if min(img.size) < 240:              # 别裁过头
            break
    return img, bands


def _watermark(img, text: str):
    try:
        from PIL import ImageDraw, ImageFont
    except ImportError:  # pragma: no cover
        return img
    draw = ImageDraw.Draw(img)
    w, h = img.size
    size = max(18, int(h * 0.045))
    font = None
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), text, font=font)
    x = w - (box[2] - box[0]) - int(w * 0.03)
    y = h - (box[3] - box[1]) - int(h * 0.04)
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 200))
    return img


def process_image(data: bytes, out_path: Path, watermark: str = "",
                  size: Optional[Tuple[int, int]] = None,
                  dew: Optional[bool] = None) -> Dict[str, Any]:
    """处理单张图片并落盘，返回尺寸 / 体积等信息。

    dew=None 时按 config.IMG_DEWATERMARK 决定是否裁边缘字样；视频抽帧可传 False
    以保留画面完整（在线视频自带水印，硬裁会糊 / 跑题）。
    """
    _require_pillow()
    from PIL import Image

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.open(io.BytesIO(data))
    img.load()

    # 隐私处理：转换为干净的 RGB 位图，EXIF / GPS / 拍摄信息随之丢弃
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    if config.IMG_STRIP_META:
        img = _strip_metadata(img)

    # 去字样：先按配置裁边并居中裁剪到目标比例，再检测裁掉压在画面边缘的文字条
    # （先裁比例再去字样，竖屏视频抽出的帧字样才会落在「边缘」上被检测到）
    img = trim_border(img, config.IMG_TRIM_BORDER)
    img = center_crop(img, config.IMG_ASPECT)
    bands: List[str] = []
    if (dew if dew is not None else config.IMG_DEWATERMARK):
        img, bands = strip_text_bands(img)

    img = img.resize(size or config.IMG_TARGET, Image.LANCZOS)

    # 产出图片默认不带任何字样；只有显式传入 watermark 才会叠加
    if watermark or config.IMG_WATERMARK:
        img = _watermark(img, watermark or config.IMG_WATERMARK)

    fmt = (config.IMG_FORMAT or "JPEG").upper()
    ext = out_path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        fmt = "JPEG"
    elif ext == ".webp":
        fmt = "WEBP"
    elif ext == ".png":
        fmt = "PNG"

    quality = config.IMG_QUALITY
    params: Dict[str, Any] = {"optimize": True}
    if fmt == "JPEG":
        params.update({"quality": quality, "progressive": True})
    elif fmt == "WEBP":
        params.update({"quality": quality, "method": 5})

    while True:
        buf = io.BytesIO()
        img.save(buf, format=fmt, **params)
        if len(buf.getvalue()) <= config.IMG_MAX_BYTES or quality <= 55:
            break
        quality -= 8
        if fmt == "JPEG":
            params["quality"] = quality
        elif fmt == "WEBP":
            params["quality"] = quality
        else:
            break
    out_path.write_bytes(buf.getvalue())

    return {
        "path": str(out_path),
        "width": img.size[0],
        "height": img.size[1],
        "bytes": out_path.stat().st_size,
        "format": fmt,
        "trimmed_bands": bands,     # 被裁掉的边缘文字条（水印 / 台标 / 字幕）
        "watermarked": bool(watermark or config.IMG_WATERMARK),
    }


def _strip_metadata(img):
    """重建像素数据，确保不携带任何 EXIF / ICC / 注释。"""
    from PIL import Image
    clean = Image.new(img.mode, img.size)
    clean.paste(img)
    return clean


def thumbnail(data: bytes, out_path: Path, size=(320, 180)) -> Optional[str]:
    """生成缩略图（用于列表预览），失败返回 None。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail(size, Image.LANCZOS)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, format="JPEG", quality=70, optimize=True)
        return str(out_path)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 视频
def _run(args: List[str], timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )


def probe(path: Path, headers: Optional[Dict[str, str]] = None,
          is_url: bool = False) -> Dict[str, Any]:
    """读取视频元信息（时长 / 分辨率 / 是否有音轨）。

    直链探测时要把 UA 和 Referer 一起带上，否则不少 CDN 直接 403，
    探不到时长（起始点永远从头算）也探不到音轨（会被误判成「无音轨」）。
    """
    args = [ffprobe_bin(), "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams"]
    if headers or is_url:
        args += _ffmpeg_input_args(headers, is_url)
    args.append(str(path))
    try:
        out = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        data = json.loads(out.stdout.decode("utf-8", "ignore") or "{}")
    except Exception:  # noqa: BLE001
        return {}
    fmt = data.get("format") or {}
    info: Dict[str, Any] = {"duration": float(fmt.get("duration") or 0), "has_audio": False}
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and "width" not in info:
            info["width"] = int(s.get("width") or 0)
            info["height"] = int(s.get("height") or 0)
        elif s.get("codec_type") == "audio":
            info["has_audio"] = True
    return info


def _audio_encode_args() -> List[str]:
    """成片音轨参数：统一 AAC / 44.1k / 双声道，平台转码最不容易挑刺。"""
    return ["-c:a", "aac", "-b:a", f"{config.VIDEO_AUDIO_BITRATE}k",
            "-ac", "2", "-ar", str(config.VIDEO_AUDIO_RATE)]


def mux_silent_audio(path: Path) -> bool:
    """给无音轨的成片补一条静音 AAC 轨（画面直接 copy，不重编码）。

    百家号转码要求必须有音轨，缺音轨会返回 {status: FAILED, code: NO_AUDIO}，
    上传环节看着像「传不上去」。失败时保留原文件不动，返回 False。
    """
    info = probe(path)
    duration = float(info.get("duration") or 0)
    tmp = path.with_name(f"{path.stem}.audio{path.suffix or '.mp4'}")
    args = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(path)]
    args += ["-f", "lavfi", "-i", f"anullsrc=r={config.VIDEO_AUDIO_RATE}:cl=stereo"]
    args += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy"] + _audio_encode_args()
    # 用片长而不是 -shortest 截断静音源：copy 视频轨时 -shortest 偶尔收不住，会生成超长空音轨
    args += ["-t", f"{duration:.2f}"] if duration > 0 else ["-shortest"]
    args += ["-movflags", "+faststart", str(tmp)]
    try:
        res = _run(args, timeout=180)
        if res.returncode == 0 and tmp.exists() and tmp.stat().st_size:
            tmp.replace(path)
            return True
    except Exception:  # noqa: BLE001
        pass
    tmp.unlink(missing_ok=True)
    return False


def _clip_start(duration: float, clip: float) -> float:
    """跳过片头：默认只跳前 2 秒（避免跑题）；VIDEO_CLIP_MIDRATIO>0 时再向中段偏移。"""
    room = duration - clip
    if room <= 0:
        return 0.0
    start = float(config.VIDEO_CLIP_START)
    mid = float(getattr(config, "VIDEO_CLIP_MIDRATIO", 0.0) or 0.0)
    if mid > 0:
        start = max(start, room * mid)
    return round(min(room, start), 2)


def _ffmpeg_input_args(headers: Optional[Dict[str, str]], is_url: bool) -> List[str]:
    args: List[str] = []
    if headers:
        from .download import ffmpeg_headers
        raw = ffmpeg_headers(headers)
        if raw:
            args += ["-headers", raw]
    if is_url:
        args += ["-user_agent", config.USER_AGENT]
    return args


def _sample_frames(src: str, start: float, clip: float, tmp: Path,
                   headers: Optional[Dict[str, str]] = None, is_url: bool = True,
                   n: int = 3) -> List[Path]:
    """在截取区间内均匀取 n 张样本帧，用于文字条投票。"""
    paths: List[Path] = []
    base = _ffmpeg_input_args(headers, is_url)
    for i in range(n):
        pos = start + clip * (i + 0.5) / n
        target = tmp / f"probe-{i}.jpg"
        args = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"] + base
        args += ["-ss", f"{pos:.2f}", "-i", src, "-frames:v", "1", "-q:v", "4", str(target)]
        try:
            res = _run(args, timeout=120)
        except Exception:  # noqa: BLE001
            continue
        if res.returncode == 0 and target.exists() and target.stat().st_size:
            paths.append(target)
    return paths


def _video_crop_expr(samples: List[Path]) -> str:
    """多帧投票：≥2/3 帧在同一边检出文字条才裁，取一致比例的最大值。

    返回 ffmpeg crop 表达式（如 crop=iw*1.000:ih*0.900:iw*0.000:ih*0.000）或空串。
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return ""
    votes: Dict[str, List[float]] = {}
    for p in samples:
        try:
            img = Image.open(p).convert("RGB")
        except Exception:  # noqa: BLE001
            continue
        frame_votes: Dict[str, float] = {}
        for _ in range(2):                       # 每帧最多去两条边
            hit = detect_text_band(img)
            if not hit:
                break
            name, ratio = hit
            frame_votes[name] = min(0.30, frame_votes.get(name, 0.0) + ratio)
            img = img.crop(_crop_out_band(img.size, _band_box(img.size, name, ratio)))
            if min(img.size) < 240:
                break
        for name, ratio in frame_votes.items():
            votes.setdefault(name, []).append(ratio)
    need = max(1, int(len(samples) * 2 / 3 + 0.5))
    crop = {name: max(rs) for name, rs in votes.items() if len(rs) >= need}
    if not crop:
        return ""
    left, right = crop.get("left", 0.0), crop.get("right", 0.0)
    top, bottom = crop.get("top", 0.0), crop.get("bottom", 0.0)
    if left + right >= 0.6 or top + bottom >= 0.6:   # 别裁过头
        return ""
    return (f"crop=iw*{1 - left - right:.3f}:ih*{1 - top - bottom:.3f}"
            f":iw*{left:.3f}:ih*{top:.3f}")


def process_video(src: str, out_path: Path, headers: Optional[Dict[str, str]] = None,
                  clip_seconds: int = 0, is_url: bool = True,
                  start: Optional[float] = None) -> Dict[str, Any]:
    """下载/截取/转码/压缩一步到位，返回元信息。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    clip = clip_seconds or config.VIDEO_CLIP_SECONDS
    if start is None:
        # ffprobe 也能直接探 URL 时长；探不到就从头截（带上 UA / Referer，CDN 才给得太元信息）
        try:
            duration = float(probe(Path(src), headers, is_url).get("duration") or 0)
        except Exception:  # noqa: BLE001
            duration = 0.0
        start = _clip_start(duration, clip)
    start = max(0.0, float(start))

    # 去字样：转码前采样几帧投票，把压在边缘的文字条（台标 / 站名水印）直接 crop 掉
    crop = ""
    if config.VIDEO_DEWATERMARK:
        try:
            with tempfile.TemporaryDirectory() as td:
                samples = _sample_frames(src, start, clip, Path(td), headers, is_url)
                crop = _video_crop_expr(samples)
        except Exception:  # noqa: BLE001
            crop = ""
    vf = ",".join(x for x in (crop, f"scale={config.VIDEO_MAX_WIDTH}:-2,fps={config.VIDEO_FPS}") if x)

    args = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
    args += _ffmpeg_input_args(headers, is_url)
    args += ["-ss", f"{start:.2f}", "-i", src]
    want_audio = not config.VIDEO_MUTE
    if want_audio:
        # 「源有就带上」：0:a:0? 的 ? 让没音轨的源不报错，出片后再补静音轨
        args += ["-map", "0:v:0", "-map", "0:a:0?"]
    args += ["-t", str(clip), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
             "-crf", str(config.VIDEO_CRF), "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if not want_audio:
        args += ["-an"]
    else:
        args += _audio_encode_args()
    args += [str(out_path)]

    res = _run(args, timeout=300)
    if res.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        msg = res.stderr.decode("utf-8", "ignore")[-300:]
        raise ProcessError(f"视频处理失败：{msg or 'ffmpeg 返回非零'}")

    info = probe(out_path)
    if want_audio and not info.get("has_audio") and getattr(config, "VIDEO_SILENT_AUDIO", True):
        # 源片本身没音轨：补一条静音轨，否则百家号等平台转码报 NO_AUDIO 传不上去
        if mux_silent_audio(out_path):
            info = probe(out_path)
    info.update({
        "path": str(out_path),
        "bytes": out_path.stat().st_size,
        "clip": clip,
        "start": start,
        "crop": crop,
        "has_audio": bool(info.get("has_audio")),
    })
    return info


def extract_frames(video_path: Path, out_dir: Path, count: int = 0,
                   polish: bool = True) -> List[Dict[str, Any]]:
    """从成片中均匀抽帧；默认按图片标准加工（统一尺寸 / 去 EXIF）。

    不裁边缘字样（VIDEO_FRAME_DEWATERMARK=False）：在线视频普遍自带台标，硬裁会
    破坏构图且二次编码后变糊；如确需处理自有片源，把开关改 True 即可。
    """
    count = count or config.VIDEO_FRAMES
    out_dir.mkdir(parents=True, exist_ok=True)
    info = probe(video_path)
    duration = float(info.get("duration") or 0)
    frames: List[Dict[str, Any]] = []
    dew = bool(getattr(config, "VIDEO_FRAME_DEWATERMARK", False))
    seen_fp: List[str] = []
    for i in range(count):
        target = _frame_path(out_dir, i + 1)
        if duration > 0:
            pos = duration * (i + 0.5) / count
            args = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{pos:.2f}",
                    "-i", str(video_path), "-frames:v", "1", "-q:v", "3", str(target)]
        else:
            args = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
                    "-vf", f"select='not(mod(n\\,{max(1, int(count))}))'", "-vsync", "vfr",
                    "-frames:v", "1", "-q:v", "3", str(target)]
        res = _run(args, timeout=120)
        if res.returncode != 0 or not target.exists():
            continue
        meta: Dict[str, Any] = {}
        if polish and config.VIDEO_FRAME_PROCESS:
            try:
                raw = target.read_bytes()
                if is_text_plate(raw):          # 片头字卡 / 纯文字帧不要
                    target.unlink(missing_ok=True)
                    continue
                if getattr(config, "MEDIA_DEDUP", True):
                    fp = image_fingerprint(raw)
                    # 抽帧间隔相近时画面几乎一样，重复帧没有素材价值
                    if duplicate_of(fp, seen_fp, max(1, getattr(config, "MEDIA_DEDUP_MAX_DIST", 6) // 2)):
                        target.unlink(missing_ok=True)
                        continue
                    seen_fp.append(fp)
                meta = process_image(raw, target, size=config.IMG_FRAME_TARGET, dew=dew)
            except Exception:  # noqa: BLE001
                meta = {}
        frames.append({"path": str(target), "index": i + 1, "bytes": target.stat().st_size,
                       "width": meta.get("width", 0), "height": meta.get("height", 0),
                       "trimmed_bands": meta.get("trimmed_bands") or []})
    return frames


def _frame_path(out_dir: Path, idx: int) -> Path:
    return out_dir / f"frame-{idx:02d}.jpg"
