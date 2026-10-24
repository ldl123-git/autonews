"""网页正文提取与内容形态判定（文章 / 图片 / 视频）。"""
import re
from html.parser import HTMLParser
from typing import Dict, List, Optional

from .config import CLEAN_TEXT, COLLECT_MEDIA

SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "form", "nav", "footer", "header", "aside"}
CONTAINER_TAGS = {"div", "section", "article", "main", "td", "body"}
BLOCK_TAGS = {"p", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "pre"}
IMG_ATTRS = ("src", "data-src", "data-original", "data-lazy-src", "data-actualsrc", "data-url", "data-image")
IMG_BLACKLIST = ("logo", "icon", "sprite", "avatar_default", "placeholder", "loading", "bg_", "qrcode")
VIDEO_EXT = (".mp4", ".m3u8", ".webm", ".mov", ".flv")
NOISE_CLASS = ("nav", "menu", "footer", "header", "sidebar", "comment", "advert", "breadcrumb",
               "pagination", "share", "related", "recommend", "copyright", "login")
PARA_MIN = 20  # 文本块被视作段落的最小字数（用于 span 型正文，如百家号）

# ---------------------------------------------------------------- 正文清洗
# 整行丢弃：站点推广、署名、版权声明、导流语等
NOISE_LINE = (
    r"返回\S{0,8}[，,]\s*查看更多",
    r"^\s*责任编辑\s*[:：]?",
    r"^\s*(责编|编辑|校对|审核|主编|撰稿|摄影|制图|剪辑|排版|签发)\s*[:：]",
    r"^\s*(来源|原标题|文章来源|稿件来源|出处|原标题为)\s*[:：]",
    r"^\s*(声明|版权声明|免责声明|特别声明|原创声明)\s*[:：]?",
    r"^\s*[（(]?\s*本文(来自|转载自|首发)",
    r"更多原创资讯请下载",
    r"未经授权.{0,12}转载",
    r"转载请(注明|标明)",
    r"(扫码|长按识别|识别二维码|扫描二维码|二维码)",
    r"(下载|打开|安装|更新)\s*\S{0,8}(APP|app|客户端)",
    r"(手机|客户端)看(新闻|资讯)",
    r"(阅读原文|查看全文|查看原文|点击阅读|了解更多|精彩推荐|推荐阅读|延伸阅读|"
    r"相关阅读|猜你喜欢|热门推荐|往期回顾|继续阅读|下一篇|上一篇)",
    r"^\s*(投稿方式|投稿邮箱|邮箱投稿|投稿请|图片投稿|视频投稿|征稿|来稿请)",
    r"(欢迎(关注|订阅)|关注我们|订阅我们|投稿邮箱|新闻热线|报料热线|爆料热线|商务合作|广告合作)",
    r"^\s*[（(]\s*[\u4e00-\u9fa5]{1,4}\s*[）)]\s*$",          # （完）（图）（注）
    r"^\s*[\W_\-—=*·•+|]{1,20}\s*$",                          # 纯符号装饰行
)

# 段落内删除的片段（就地抹掉，不留空行）
NOISE_FRAGMENT = (
    r"[（(]\s*本文(来自|转载自)[^）)]*[）)]",
    r"[（(]\s*(编辑|责任编辑|责编|校对)[：:][^）)]*[）)]",
    r"更多原创资讯请下载[^，。！？；]*",
    r"(版权声明|免责声明|特别声明)[：:]?[^。！？]{0,60}[。！？]?",
    r"未经授权[^。！？]{0,40}[。！？]?",
    r"转载(请|须)[^。！？]{0,30}[。！？]?",
)

# 只作用于文末的片段（署名、站名尾巴等）
TAIL_FRAGMENT = (
    r"\s*[\u4e00-\u9fa5]{0,10}(记者|编辑|通讯员|报道员|撰文)\s*[\u4e00-\u9fa5A-Za-z·]{1,8}\s*$",
    r"\s*[（(][^（()）]{0,60}[）)]\s*$",
    r"\s*[-—_=·•]{2,}\s*$",
)

_BYLINE_RE = re.compile(
    r"^[\u4e00-\u9fa5A-Za-z0-9·\s]{0,16}(记者|编辑|通讯员|报道员|撰文|文图|图文)"
    r"\s*[\u4e00-\u9fa5A-Za-z·\s]{0,8}$"
)

# 推荐信息流的小标题（出现即截掉其后所有内容）
RECOMMEND_MARKERS = (
    "推荐阅读", "精彩推荐", "相关阅读", "相关文章", "相关推荐", "延伸阅读", "扩展阅读",
    "猜你喜欢", "热门推荐", "热门文章", "往期推荐", "往期回顾", "大家都在看", "大家还在看",
    "更多精彩", "更多资讯", "继续阅读", "为你推荐", "推荐关注", "看了又看", "热门资讯",
)


def detect_encoding(raw: bytes, content_type: str = "") -> str:
    m = re.search(r'charset=(["\']?)([\w\-]+)', content_type or "", re.I)
    if m:
        return m.group(2).lower()
    head = raw[:2048].decode("ascii", errors="ignore")
    m = re.search(r'<meta[^>]+charset=(["\']?)([\w\-]+)', head, re.I)
    if m:
        return m.group(2).lower()
    for enc in ("utf-8", "gb18030"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


class _PageParser(HTMLParser):
    def __init__(self, collect_media: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.collect_media = collect_media
        self.stack: List[Dict] = []
        self.images: List[str] = []
        self.videos: List[str] = []
        self.img_count = 0
        self.video_count = 0
        self.title = ""
        self.meta: Dict[str, str] = {}
        self.candidates: List[Dict] = []
        self._in_title = False
        self._text_buf: List[str] = []

    # ---- helpers ----
    def _attr(self, attrs, name: str) -> str:
        for k, v in attrs:
            if k and k.lower() == name:
                return (v or "").strip()
        return ""

    def _skipping(self) -> bool:
        return any(node["tag"] in SKIP_TAGS for node in self.stack)

    # ---- handlers ----
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self.stack.append({"tag": tag, "attrs": attrs, "texts": [], "paras": [],
                           "cls": self._attr(attrs, "class")})
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            key = self._attr(attrs, "property") or self._attr(attrs, "name")
            if key:
                self.meta[key.lower()] = self._attr(attrs, "content")
            return
        if tag == "img":
            for name in IMG_ATTRS:
                src = self._attr(attrs, name)
                if self._valid_image(src):
                    self.img_count += 1
                    if self.collect_media:
                        self.images.append(src)
                    break
            return
        if tag in ("video", "source", "embed"):
            src = self._attr(attrs, "src")
            if src and self._valid_video(src):
                self.video_count += 1
                if self.collect_media:
                    self.videos.append(src)
            return
        if tag == "iframe":
            src = self._attr(attrs, "src")
            if src and ("video" in src.lower() or "player" in src.lower()):
                self.video_count += 1
                if self.collect_media:
                    self.videos.append(src)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                nodes = self.stack[i:]
                del self.stack[i:]
                break
        else:
            return
        for node in reversed(nodes):
            text = re.sub(r"\s+", " ", "".join(node["texts"])).strip()
            is_container = node["tag"] in CONTAINER_TAGS
            if is_container:
                node["paras"] = _merge_overlap([p for p in node["paras"] if p])
                container_text = "\n".join(node["paras"])
                if node["tag"] != "body":
                    if node["paras"]:
                        self.candidates.append({
                            "text": container_text,
                            "pcount": len(node["paras"]),
                            "cls": node["cls"],
                            "kind": "paras",
                        })
                    elif len(text) >= 150:
                        self.candidates.append({
                            "text": text, "pcount": 1, "cls": node["cls"], "kind": "raw",
                        })
                    if self.stack and self.stack[-1]["tag"] in CONTAINER_TAGS:
                        self.stack[-1]["paras"].extend(node["paras"])
                if len(container_text) > len(text):
                    text = container_text
            if text and self.stack:
                parent = self.stack[-1]
                parent_is_container = parent["tag"] in CONTAINER_TAGS
                is_para = node["tag"] == "p" or (node["tag"] == "span" and len(text) >= PARA_MIN)
                if is_para and parent_is_container:
                    parent["paras"].append(text)
                parent["texts"].append(text)
        if not self.stack:
            joined = re.sub(r"\s+", " ", "".join(nodes[0]["texts"])).strip()
            if joined:
                self._text_buf.append(joined)

    def handle_data(self, data):
        if not self.stack:
            return
        if self._in_title:
            self.title += data
        if self._skipping():
            return
        text = data.strip()
        if text:
            self.stack[-1]["texts"].append(text)

    # ---- validators ----
    def _valid_image(self, src: str) -> bool:
        if not src or src.startswith("data:"):
            return False
        low = src.lower()
        if any(k in low for k in IMG_BLACKLIST):
            return False
        if low.endswith((".gif",)) and "loading" in low:
            return False
        return low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")) or "image" in low

    def _valid_video(self, src: str) -> bool:
        if not src or src.startswith("data:"):
            return False
        low = src.lower()
        return low.endswith(VIDEO_EXT) or "m3u8" in low or "/video" in low

    # ---- result ----
    def _pick_main_text(self) -> str:
        """取文本量最大的正文容器（优先含段落的容器），避免把导航、页脚、标题混进正文。"""
        usable = [c for c in self.candidates if not _is_noise_class(c["cls"])]
        if not usable:
            usable = list(self.candidates)
        para_cands = [c for c in usable if c["kind"] == "paras"]
        if para_cands:
            # 在文本最长的容器中，选被长容器包含且文本比例不低于 78% 的最小容器（即最紧致）
            para_cands.sort(key=lambda c: len(c["text"]), reverse=True)
            best = para_cands[0]
            for c in para_cands[1:]:
                if not c["text"] or len(c["text"]) < 80:
                    break
                if c["text"] in best["text"] and len(best["text"]) <= 1.28 * len(c["text"]):
                    best = c
            return best["text"].strip()
        raw_cands = [c for c in usable if c["kind"] == "raw"]
        if raw_cands:
            raw_cands.sort(key=lambda c: len(c["text"]), reverse=True)
            return raw_cands[0]["text"].strip()
        fallback = ""
        for text in self._text_buf:
            if len(text) > len(fallback):
                fallback = text
        for node in self.stack:
            text = re.sub(r"\s+", " ", "".join(node["texts"])).strip()
            if len(text) > len(fallback):
                fallback = text
        return fallback

    def result(self) -> Dict:
        title = (self.meta.get("og:title") or self.meta.get("twitter:title") or self.title or "").strip()
        images = _dedup(self.images)[:40] if self.collect_media else []
        videos = _dedup(self.videos)[:10] if self.collect_media else []
        return {
            "title": title,
            "text": self._pick_main_text(),
            "images": images,
            "videos": videos,
            "img_count": self.img_count,
            "video_count": self.video_count,
            "meta": self.meta,
        }


def _is_noise_class(cls: str) -> bool:
    low = (cls or "").lower()
    return any(k in low for k in NOISE_CLASS)


_NOISE_LINE_RE = [re.compile(p) for p in NOISE_LINE]
_NOISE_FRAG_RE = [re.compile(p) for p in NOISE_FRAGMENT]
_TAIL_FRAG_RE = [re.compile(p) for p in TAIL_FRAGMENT]


def _is_noise_line(line: str) -> bool:
    return any(r.search(line) for r in _NOISE_LINE_RE)


def _is_byline(line: str) -> bool:
    """署名行，如「澎湃新闻记者 王健」——只在文末判定，避免误伤正文。"""
    if len(line) > 22 or re.search(r"[，。！？；、,!?;]", line):
        return False
    return bool(_BYLINE_RE.match(line))


def _is_tail_junk(line: str) -> bool:
    """文末短碎句：没有句末标点且很短，多为站名 / 署名残留。"""
    if re.search(r"[。！？；.!?;]", line):
        return False
    return len(line) <= 20


def _trim_tail(lines: List[str]) -> None:
    while len(lines) > 1:
        last = lines[-1]
        if _is_noise_line(last) or _is_byline(last) or _is_tail_junk(last):
            lines.pop()
            continue
        break


def _is_strong_headline(line: str) -> bool:
    """推荐位标题：短句且以 ？/！/→ 结尾。"""
    s = line.rstrip()
    return len(s) <= 70 and s.endswith(("？", "?", "！", "!", "→", ">>"))


def _is_feed_like(line: str) -> bool:
    """信息流条目：栏目名（很短）或标题句。"""
    s = line.rstrip()
    return len(s) <= 12 or _is_strong_headline(s)


def _drop_recommendations(lines: List[str]) -> None:
    """截掉文末的「推荐阅读 / 相关资讯」信息流。"""
    if len(lines) < 4:
        return
    # 1) 出现推荐栏目标记：从标记处全部截掉
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if len(line) <= 20 and any(m in line for m in RECOMMEND_MARKERS):
            del lines[i:]
            break
    # 2) 无标记时按「标题行密度」找信息流起点
    total = sum(len(x) for x in lines) or 1
    cut = None
    for i in range(1, len(lines) - 2):
        if not _is_feed_like(lines[i]):
            continue
        suffix = lines[i:]
        if len(suffix) < 3:
            continue
        strong = sum(1 for x in suffix if _is_strong_headline(x))
        feed = sum(1 for x in suffix if _is_feed_like(x))
        if strong < 2 or feed < len(suffix) * 0.6:
            continue
        if sum(len(x) for x in suffix) > total * 0.4:  # 防止误伤正文主体
            continue
        cut = i
    if cut is not None:
        del lines[cut:]
    # 3) 收尾：连续的信息流短行继续清掉
    while len(lines) > 2 and _is_feed_like(lines[-1]) and len(lines[-1]) <= 12:
        lines.pop()


def clean_text(text: str) -> str:
    """清洗正文：去掉站点推广、编辑署名、版权声明等噪声，并去除重复段落。"""
    if not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    for rgx in _NOISE_FRAG_RE:
        s = rgx.sub("", s)

    lines: List[str] = []
    seen = set()
    for raw in s.split("\n"):
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if not line or _is_noise_line(line) or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    _trim_tail(lines)
    _drop_recommendations(lines)
    _trim_tail(lines)

    out = "\n".join(lines).strip()
    for rgx in _TAIL_FRAG_RE:
        out = rgx.sub("", out).rstrip()
    return out.strip()


def _merge_overlap(paras: List[str]) -> List[str]:
    """去掉被其它段落完全包含的重复段落（span 嵌套场景）。"""
    result: List[str] = []
    for p in paras:
        if any(p in r for r in result):
            continue
        result = [r for r in result if r not in p]
        result.append(p)
    return result


def _dedup(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_page(html: str, base_url: str = "", collect_media: bool = COLLECT_MEDIA,
               clean: bool = CLEAN_TEXT) -> Dict:
    parser = _PageParser(collect_media=collect_media)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - 容错：HTML 不规范时保留已解析部分
        pass
    data = parser.result()
    data["images"] = [_abs_url(u, base_url) for u in data["images"]]
    data["videos"] = [_abs_url(u, base_url) for u in data["videos"]]
    if clean:
        data["text"] = clean_text(data["text"])
    return data


def _abs_url(url: str, base: str) -> str:
    if not url or not base:
        return url
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        m = re.match(r"(https?://[^/]+)", base)
        return (m.group(1) if m else "") + url
    return url


def classify(declared: str, text: str, img_count: int = 0, video_count: int = 0,
             meta: Optional[Dict] = None) -> str:
    """判定内容形态：文章 / 图片 / 视频（只依据数量与页面特征，不依赖媒体地址）。"""
    meta = meta or {}
    og_type = (meta.get("og:type") or "").lower()
    text_len = len(re.sub(r"\s+", "", text or ""))
    if video_count or "video" in og_type:
        return "视频"
    if declared == "视频":
        return "视频"
    if text_len < 200 and img_count >= 3:
        return "图片"
    if text_len < 50 and img_count:
        return "图片"
    if declared == "图片" and text_len < 300:
        return "图片"
    return "文章"


def summarize(text: str, limit: int = 120) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    return s[:limit] + ("…" if len(s) > limit else "")
