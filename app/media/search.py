"""素材搜索：从「非原平台」的其它平台 / 图库检索图片与视频。

设计要点
1. 检索词以标题为主（标题主体 → 标题核心短语 → 正文关键词兜底），见 keywords.py。
2. 搜索结果按域名判定平台，凡命中原文平台的候选一律丢弃（跨平台取材）。
3. 图片分三档取：①按标题搜同源报道页、取该新闻的真实配图（匹配度最高）；
   ②百度 / 必应图片检索（参照 icrawler，带尺寸 / 类型 / 版式过滤，见 engines.py）；
   ③通用图库兜底。
"""
import asyncio
import html as _html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from .. import config, extract
from . import engines, keywords, platforms


@dataclass
class MediaItem:
    kind: str                    # image / video
    url: str                     # 直链
    page_url: str = ""           # 来源页面
    title: str = ""
    platform: str = ""
    provider: str = ""
    keyword: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)


def _headers(referer: str = "") -> Dict[str, str]:
    h = {
        "User-Agent": config.USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "*/*",
    }
    if referer:
        h["Referer"] = referer
    return h


# ---------------------------------------------------------------- 图片源
# 百度 / 必应的检索与过滤实现见 engines.py（参照 icrawler 的 builtin/baidu.py、bing.py）
async def _bing_images(client: httpx.AsyncClient, query: str, limit: int) -> List[MediaItem]:
    return await engines.bing_images(client, query, limit)


async def _baidu_images(client: httpx.AsyncClient, query: str, limit: int) -> List[MediaItem]:
    return await engines.baidu_images(client, query, limit)


async def _pexels_images(client: httpx.AsyncClient, query: str, limit: int) -> List[MediaItem]:
    if not config.PEXELS_API_KEY:
        return []
    try:
        resp = await client.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": max(limit, 5), "orientation": "landscape"},
            headers={"Authorization": config.PEXELS_API_KEY},
            timeout=config.MEDIA_TIMEOUT,
        )
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for p in data.get("photos") or []:
        src = (p.get("src") or {}).get("large2x") or (p.get("src") or {}).get("original") or ""
        if not src:
            continue
        out.append(MediaItem(
            kind="image", url=src, page_url=p.get("url") or "", title=p.get("alt") or "",
            platform="Pexels", provider="pexels", keyword=query,
            width=p.get("width") or 0, height=p.get("height") or 0,
        ))
    return out


async def _unsplash_images(client: httpx.AsyncClient, query: str, limit: int) -> List[MediaItem]:
    if not config.UNSPLASH_ACCESS_KEY:
        return []
    try:
        resp = await client.get(
            "https://api.unsplash.com/search/photos",
            params={"query": query, "per_page": max(limit, 5)},
            headers={"Client-ID": config.UNSPLASH_ACCESS_KEY},
            timeout=config.MEDIA_TIMEOUT,
        )
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for p in data.get("results") or []:
        src = (p.get("urls") or {}).get("regular") or ""
        if not src:
            continue
        out.append(MediaItem(
            kind="image", url=src, page_url=(p.get("links") or {}).get("html") or "",
            title=p.get("alt_description") or "", platform="Unsplash", provider="unsplash",
            keyword=query, width=p.get("width") or 0, height=p.get("height") or 0,
        ))
    return out


IMAGE_PROVIDERS = {
    "baidu": _baidu_images,
    "bing": _bing_images,
    "pexels": _pexels_images,
    "unsplash": _unsplash_images,
}


# ---------------------------------------------------------------- 视频源
_BILI_COOKIE: Dict[str, str] = {}


async def _bili_cookie(client: httpx.AsyncClient) -> Dict[str, str]:
    """B 站搜索接口对「裸请求」会返回非 JSON（风控页 / 空响应），先取一次站点 cookie。

    只在首次调用时请求一次，之后整个进程复用。
    """
    if _BILI_COOKIE:
        return _BILI_COOKIE
    try:
        resp = await client.get("https://www.bilibili.com/",
                                headers=_headers(), timeout=min(config.MEDIA_TIMEOUT, 15.0))
        for name, value in resp.cookies.items():
            if name:
                _BILI_COOKIE[name] = value
    except Exception:  # noqa: BLE001
        pass
    _BILI_COOKIE.setdefault("buvid3", _uuid_like())
    return _BILI_COOKIE


def _uuid_like() -> str:
    import uuid
    return uuid.uuid4().hex[:32] + "infoc"


async def _bili_search(client: httpx.AsyncClient, query: str) -> Dict[str, Any]:
    """带 cookie 的 B 站视频搜索，失败重试两次 —— 偶发的非 JSON 响应是「搜不到
    视频」的主要成因，不能让它静默变成零结果。"""
    cookie = await _bili_cookie(client)
    headers = _headers("https://search.bilibili.com/")
    headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookie.items())
    last = {}
    for attempt in range(3):
        try:
            resp = await client.get(
                "https://api.bilibili.com/x/web-interface/search/type",
                params={"search_type": "video", "keyword": query, "page": 1},
                headers=headers,
                timeout=config.MEDIA_TIMEOUT,
            )
            text = resp.text
            data = json.loads(text) if text else {}
            if isinstance(data, dict) and data.get("code") in (0, "0"):
                return data
            last = data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            pass
        if attempt < 2:
            await asyncio.sleep(0.6 * (attempt + 1))
    return last


async def _bilibili_videos(client: httpx.AsyncClient, query: str, limit: int) -> List[MediaItem]:
    """B 站搜索（公开接口），返回 bvid 等信息；真实下载地址在 collector 里二次解析。"""
    data = await _bili_search(client, query)
    if not data:
        return []
    payload = (data.get("data") or {})
    rows = payload.get("result") or payload.get("item") or []
    out = []
    for r in rows[: max(limit * 3, 12)]:
        bvid = r.get("bvid") or ""
        if not bvid:
            continue
        duration = _parse_duration(r.get("duration") or "")
        out.append(MediaItem(
            kind="video", url="", page_url=f"https://www.bilibili.com/video/{bvid}",
            title=_strip_tags(r.get("title") or ""), platform="哔哩哔哩", provider="bilibili",
            keyword=query, duration=duration,
            extra={
                "bvid": bvid,
                "cover": ("https:" + r["pic"]) if str(r.get("pic") or "").startswith("//") else (r.get("pic") or ""),
                "author": r.get("author") or "",
            },
        ))
    return out


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _parse_duration(s: str) -> float:
    try:
        parts = [int(x) for x in str(s).split(":")]
    except ValueError:
        return 0.0
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


async def _pexels_videos(client: httpx.AsyncClient, query: str, limit: int) -> List[MediaItem]:
    if not config.PEXELS_API_KEY:
        return []
    try:
        resp = await client.get(
            "https://api.pexels.com/videos/search",
            params={"query": query, "per_page": max(limit, 3), "orientation": "landscape"},
            headers={"Authorization": config.PEXELS_API_KEY},
            timeout=config.MEDIA_TIMEOUT,
        )
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for v in data.get("videos") or []:
        best = None
        for f in v.get("video_files") or []:
            if f.get("file_type") != "video/mp4":
                continue
            w = f.get("width") or 0
            if best is None or (w <= 1280 and w > (best.get("width") or 0)):
                best = f
        if not best:
            continue
        out.append(MediaItem(
            kind="video", url=best.get("link") or "", page_url=v.get("url") or "",
            title=v.get("url") or "", platform="Pexels", provider="pexels", keyword=query,
            width=best.get("width") or 0, height=best.get("height") or 0,
            duration=float(v.get("duration") or 0),
        ))
    return out


VIDEO_PROVIDERS = {
    "bilibili": _bilibili_videos,
    "pexels": _pexels_videos,
}


# ---------------------------------------------------------------- 标题相关度
_GRAM_CHARS = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]+")


def _grams(text: str, n: int = 2) -> set:
    """字符 n-gram 集合（只保留中日韩 / 字母数字，忽略标点与空白）。"""
    s = "".join(_GRAM_CHARS.findall(text or "")).lower()
    if not s:
        return set()
    if len(s) <= n:
        return {s}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _clean_title(s: str) -> str:
    """标题纯文本：去标点 / 空白 / 常见站点尾巴，供字面比对。"""
    s = re.sub(r"[“”\"'‘’《》〈〉「」『』【】\[\]()（）|｜·、，。！？；;：:!?\s]", "", s or "")
    return s.lower()


def _lcs_ratio(a: str, b: str) -> float:
    """最长公共子串占较短标题的比例：同源报道的标题往往整段重合。"""
    if not a or not b:
        return 0.0
    shortest = min(len(a), len(b))
    if shortest == 0:
        return 0.0
    best = 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
        if best >= shortest:
            break
    return best / shortest


def match_score(item: MediaItem, title: str, terms: List[str] = ()) -> float:
    """素材与原文的相关度 0~1 = 标题字面相似度 + 核心词命中率 + 连续子串重合度。

    三个信号互补：
    - dice：标题主体（≤40 字）的字符 2-gram 重合度，衡量「说的是同一件事」；
    - term：标题核心实体在素材标题 / 页面里的命中率，防止同名百科误判；
    - lcs ：完整标题的最长公共子串占比，同源报道标题整段重合时会很高。
    三者加权后用于排序与过滤，把泛化图库结果压到后面。
    """
    if not title:
        return 0.0
    a = _grams(keywords.title_head(item.title, limit=40) or item.title or "")
    b = _grams(keywords.title_head(title, limit=40) or title)
    dice = (2 * len(a & b) / (len(a) + len(b))) if (a and b) else 0.0
    lcs = _lcs_ratio(_clean_title(title), _clean_title(item.title or ""))
    term = 0.0
    if terms:
        text = f"{item.title or ''} {item.page_url or ''}"
        hit = sum(1 for t in terms if len(t) >= 2 and t in text)
        term = hit / len(terms)
    return round(min(1.0, 0.45 * dice + 0.35 * term + 0.20 * lcs), 3)


def tail_lcs_len(title: str, other: str, skip: int = 0) -> int:
    """标题**后半段**与另一标题的最长公共子串长度。

    主语通常在标题开头（「金正恩携女儿…」「武汉一女子…」），只看整体重合会把
    「同一个主角、另一件事」的视频算成相关。跳过开头若干字后还能对上连续片段，
    说明撞的是事件本身而不是同一张脸。
    """
    skip = skip or int(getattr(config, "MEDIA_VIDEO_TITLE_SKIP", 8) or 0)
    tail = _clean_title(title)[skip:]
    return _lcs_len(tail, _clean_title(other)) if len(tail) >= 2 else 0


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（绝对长度，供饱和计分使用）。"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def video_match(item: MediaItem, title: str, entities: List[str] = (),
                strong: Optional[List[str]] = None) -> float:
    """视频专用相关度：**事件实体命中率**为主，字面重合度为辅。

    视频标题（尤其 UP 主投稿）与新闻标题的措辞差异极大
    （新闻「蔷琶逼近警戒线」vs 视频「台风蔷琶逼近浙江，不同人 be like」），
    只比字面会把真正相关的视频误杀。主判据因此改成：
    - strong：标题引号里的事件专名（蔷琶 / 务工敲诈）—— 提到它基本就是同一件事；
    - entities：标题其余实词的覆盖率，排除只蹭了个地名的无关视频；
    再叠加标题字面 dice 与公共子串做细粒度区分。
    """
    if not title:
        return 0.0
    strong_list = keywords.strong_entities(title) if strong is None else list(strong)
    text = f"{item.title or ''} {item.page_url or ''}"

    def hits(words: List[str]) -> int:
        return sum(1 for w in words if len(w) >= 2 and w in text)

    cover_strong = (hits(strong_list) / len(strong_list)) if strong_list else 0.0
    cover_all = (hits(list(entities)) / len(entities)) if entities else 0.0
    a = _grams(keywords.title_head(item.title, limit=40) or item.title or "")
    b = _grams(keywords.title_head(title, limit=40) or title)
    dice = (2 * len(a & b) / (len(a) + len(b))) if (a and b) else 0.0
    # 公共子串按绝对长度饱和计分：长标题下的比例会被短而关键的片段拉平
    lcs_score = min(1.0, _lcs_len(_clean_title(title), _clean_title(item.title or "")) / 6.0)
    return round(min(1.0, 0.45 * cover_strong + 0.20 * cover_all
                     + 0.15 * dice + 0.20 * lcs_score), 3)


def _stamp(items: List[MediaItem], title: str, terms: List[str]) -> None:
    """把相关度写进 extra.match，供排序与素材清单展示。"""
    for it in items:
        it.extra["match"] = match_score(it, title, terms)


def _keep_relevant(items: List[MediaItem], limit: int, strict: bool = False) -> List[MediaItem]:
    """只留与标题相关的候选。

    默认：相关素材能凑够一半配额就丢掉不相关的；一条都不够则原样返回，避免筛成空。
    strict（关键词层结果）：只要有一条相关的就只保留相关的 —— 关键词检索出的
    不相干内容比缺素材更糟。
    """
    good = [it for it in items if it.extra.get("match", 0.0) >= config.MEDIA_MATCH_MIN]
    if strict:
        return good or items
    need = max(1, (max(limit, 1) + 1) // 2)
    return good if len(good) >= need else items


# ---------------------------------------------------------------- 同源报道页
@dataclass
class NewsPage:
    url: str = ""
    title: str = ""
    platform: str = ""
    score: float = 0.0          # 报道标题与原文标题的相关度


_META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
_META_IMAGE_KEYS = ("og:image", "og:image:url", "twitter:image", "twitter:image:src")
# 站点装饰图 / 功能图标：文件名特征 + 资源路径特征
_BAD_IMG = re.compile(
    r"(logo|icon|sprite|avatar|placeholder|loading|banner|btn|button|arrow|"
    r"qrcode|rqcode|scalecode|barcode|erweima|wechat|weibo|share|report|beian|"
    r"blank\.|default\.|user[-_]?\d|"
    r"aichat|psstatic|basics/|copy_|search_|close[_.]|refresh|nav_|tab_)", re.I)
_BAD_PATH = re.compile(r"/(_next|static|assets|dist|skin|themes|common)/|/(icon|logo|bg)s?/", re.I)
# 带字样的图：水印 / 台标 / 公众号二维码 / 字幕条，文件名或路径里通常有痕迹
_WM_IMG = re.compile(
    r"(watermark|water_mark|shuiyin|shui_yin|水印|带字|logo|brand|qrcode|erweima|"
    r"gongzhonghao|公众号|follow|subscribe)", re.I)
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif")

# 视频站 / 视频页：这些页面里的「图片」几乎都是视频封面、栏目片头、主播出镜帧，
# 而不是新闻现场照片。素材采到它们就是「图不对文」，必须从来源上排除。
VIDEO_SITE_HOSTS = (
    "bilibili.com", "biliimg.com", "bilivideo.com", "hdslb.com",
    "ixigua.com", "douyin.com", "iesdouyin.com", "kuaishou.com",
    "haokan.baidu.com", "v.qq.com", "youku.com", "iqiyi.com",
    "v.ifeng.com", "tv.sohu.com", "mgtv.com", "xinpianchang.com",
    "weishi.qq.com", "pipix.com", "ixigua.com",
)

# 视频页标题里的栏目包装痕迹（片头 / 台标 / 预告 / 花絮）
_GRAPHIC_TITLE = re.compile(
    r"(片头|片花|台标|栏目|logo|LOGO|预告|花絮|直播回放|宣传片|开场|结尾|"
    r"字幕|标题卡|海报|海报图)", re.I)


def is_video_site(url: str) -> bool:
    """是否属于视频站（其页面配图多为视频封面 / 栏目包装）。"""
    host = platforms.host_of(url)
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in VIDEO_SITE_HOSTS)


def looks_like_graphic(page_title: str, img_url: str = "") -> bool:
    """按图注 / URL 判定「品牌包装类」图片（片头卡、台标、栏目 logo）。"""
    text = f"{page_title or ''} {img_url or ''}"
    return bool(_GRAPHIC_TITLE.search(text))


# 同源报道检索引擎：Bing 网页搜索对中文长标题已基本失效（只按首字召回百科），
# 改用百度新闻 + 360 搜索，两者对中文事件的召回都很好，且结果里带真实地址。
_RESULT_H3 = re.compile(r"<h3\b[^>]*>(.*?)</h3>", re.S | re.I)
# 真实地址：360 写在 data-mdurl、百度写在 mu，都没有才退回 href
_REAL_URL = re.compile(r'''(?:data-mdurl|mu|href)\s*=\s*["']([^"']+)["']''', re.I)
# 搜索引擎自己的跳转链接，取不到真实报道页
_JUMP_HOSTS = ("baidu.com/link", "www.baidu.com", "so.com/link", "www.so.com",
               "haosou.com", "bing.com/ck")


def _real_link(block: str) -> str:
    """从结果块里取出真实文章地址。"""
    for u in _REAL_URL.findall(block or ""):
        u = _html.unescape(u)
        if not u.startswith("http"):
            continue
        host = platforms.host_of(u)
        if any(host == h or host.endswith("." + h) for h in _JUMP_HOSTS):
            continue
        return u
    return ""


def _parse_results(body: str) -> List[NewsPage]:
    """通用结果解析：<h3> 块 = 标题 + 真实地址。"""
    out: List[NewsPage] = []
    seen = set()
    for block in _RESULT_H3.findall(body or ""):
        link = _real_link(block)
        if not link or link in seen:
            continue
        title = _html.unescape(_strip_tags(block))
        if not title:
            continue
        seen.add(link)
        out.append(NewsPage(url=link, title=title, platform=platforms.platform_of_url(link)))
    return out


async def _baidu_news(client: httpx.AsyncClient, query: str, limit: int) -> List[NewsPage]:
    """百度新闻检索（tn=news，rtt=1 按时间排序）。"""
    url = ("https://www.baidu.com/s?rtt=1&bsst=1&cl=2&tn=news&wd="
           + quote(query) + "&rn=%d" % max(limit * 2, 20))
    return _parse_results(await _fetch_html(client, url))


async def _so360_news(client: httpx.AsyncClient, query: str, limit: int) -> List[NewsPage]:
    """360 搜索：中文事件召回好，结果里带 data-mdurl 真实地址。"""
    url = "https://www.so.com/s?q=" + quote(query)
    return _parse_results(await _fetch_html(client, url))


NEWS_PROVIDERS = {
    "baidu": _baidu_news,
    "so360": _so360_news,
}


async def _news_pages(client: httpx.AsyncClient, query: str, limit: int) -> List[NewsPage]:
    """按查询词找报道页：依次走各引擎，先出结果就够用。"""
    out: List[NewsPage] = []
    for name in config.NEWS_SEARCH_PROVIDERS:
        fn = NEWS_PROVIDERS.get(name)
        if not fn:
            continue
        rows = await fn(client, query, limit)
        if not rows:
            continue
        out.extend(rows)
        if len(out) >= max(limit, 5):
            break
    return out


def _meta_images(html: str) -> List[str]:
    """取 og:image / twitter:image —— 通常是这条新闻的主图。"""
    out: List[str] = []
    for tag in _META_TAG.findall(html or ""):
        m = re.search(r'(?:property|name)=["\']([^"\']+)["\']', tag, re.I)
        key = (m.group(1) if m else "").lower()
        if key not in _META_IMAGE_KEYS:
            continue
        mc = re.search(r'content=["\']([^"\']+)["\']', tag)
        if mc:
            out.append(mc.group(1))
    return out


def _abs(url: str, base: str) -> str:
    if not url:
        return ""
    url = _html.unescape(url.strip())
    if url.startswith("http"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        m = re.match(r"(https?://[^/]+)", base or "")
        return (m.group(1) if m else "") + url
    return url


def _has_og_video(html: str) -> bool:
    """页面是否声明了视频（og:type=video / og:video:*）—— 说明头图是视频封面。"""
    for tag in _META_TAG.findall(html or ""):
        m = re.search(r'(?:property|name)=["\']([^"\']+)["\']', tag, re.I)
        if not m:
            continue
        key = m.group(1).lower()
        if key.startswith("og:video") or key in ("og:type",):
            content = re.search(r'content=["\']([^"\']+)["\']', tag, re.I)
            value = (content.group(1) if content else "").lower()
            if key.startswith("og:video") or value.startswith("video"):
                return True
    return False


def _is_bad_image(url: str) -> bool:
    """排除 logo / 二维码 / 备案图标等站点装饰图，以及非图片链接。"""
    low = (url or "").lower()
    if _BAD_IMG.search(low) or _BAD_PATH.search(low) or _WM_IMG.search(low):
        return True
    return not (low.endswith(_IMG_EXT) or "image" in low)


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await client.get(url, headers=_headers("https://www.bing.com/"),
                                timeout=config.MEDIA_TIMEOUT, follow_redirects=True)
        if resp.status_code >= 400:
            return ""
        raw = resp.content
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype and not (ctype.startswith("text/") or "html" in ctype or "xml" in ctype):
            return ""
        return raw.decode(extract.detect_encoding(raw, ctype), errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


async def _page_images(client: httpx.AsyncClient, page: NewsPage, limit: int = 5) -> List[MediaItem]:
    """从报道页取这条新闻的真实配图：og:image 优先，其次正文图。"""
    if is_video_site(page.url) or looks_like_graphic(page.title):
        return []          # 视频站 / 栏目页的配图是封面与片头，不能当新闻照片
    html = await _fetch_html(client, page.url)
    if not html:
        return []
    try:
        data = extract.parse_page(html, page.url, collect_media=True, clean=False)
    except Exception:  # noqa: BLE001
        data = {}
    is_video_page = bool(data.get("videos")) or _has_og_video(html)
    urls = [] if is_video_page else [_abs(u, page.url) for u in _meta_images(html)]
    urls += list(data.get("images") or [])

    items: List[MediaItem] = []
    seen = set()
    for u in urls:
        if not u or u in seen or _is_bad_image(u):
            continue
        if looks_like_graphic(page.title, u):
            continue
        seen.add(u)
        items.append(MediaItem(
            kind="image", url=u, page_url=page.url, title=page.title,
            platform=page.platform or platforms.platform_of_url(u),
            provider="news", keyword="",
            extra={"match": page.score, "source_title": page.title},
        ))
        if len(items) >= limit:
            break
    return items


async def search_news_images(client: httpx.AsyncClient, title: str, queries: List[str],
                             limit: int, exclude_platform: str = "") -> List[MediaItem]:
    """按标题找同源报道 → 取该新闻的真实配图（匹配度最高的通道）。

    与图片搜索引擎不同：这里拿到的是同一事件的其它报道所用的图，
    而不是「仙人跳」「防癌神药」这类关键词的概念图 / 素材图。
    """
    if not title:
        return []
    terms = keywords.title_terms(title)
    pages: List[NewsPage] = []
    seen = set()
    for q in (queries or [title])[:2]:
        for p in await _news_pages(client, q, limit):
            if p.url in seen:
                continue
            seen.add(p.url)
            pages.append(p)

    # 强词：长度 ≥3 的核心实体（如「防癌神药」「仙人跳」），是判定同源的硬凭据。
    # 只用字面相似度会把「成本（经济学名词）_百度百科」这类同名高频页误判成同源。
    strong = [t for t in terms if len(t) >= 3] or terms
    scored: List[NewsPage] = []
    for p in pages:
        if platforms.is_same(p.platform, exclude_platform):
            continue                     # 跨平台取材：跳过原平台的报道
        p.score = match_score(MediaItem(kind="page", url=p.url, title=p.title), title, terms)
        if p.score < config.MEDIA_NEWS_MATCH_MIN:
            continue                     # 字面都不像，不是同一件事
        if not any(t in p.title for t in strong):
            continue                     # 标题里没有核心实体，多半只是同名百科 / 政务页
        scored.append(p)
    scored.sort(key=lambda p: -p.score)

    out: List[MediaItem] = []
    for p in scored[: max(limit, 3)]:
        out.extend(await _page_images(client, p, limit=max(limit, 3)))
        if len(out) >= limit * 2:
            break
    return out[: max(limit * 2, 6)]


async def origin_images(client: httpx.AsyncClient, url: str, title: str,
                        platform: str = "", limit: int = 6) -> List[MediaItem]:
    """原文自带配图：就是这篇文章自己的图，相关性最高、覆盖率也最高。

    很多小新闻（头条 / 百家号）在搜索引擎里搜不到同源报道，此时原文配图
    是唯一能保证「图不对文」不发生的素材来源。可用 config.MEDIA_USE_ORIGIN_IMAGES 关闭。
    """
    if not url:
        return []
    if is_video_site(url):
        return []          # 视频页里的图是封面 / 片头帧，不是新闻照片
    html = await _fetch_html(client, url)
    if not html:
        return []
    try:
        data = extract.parse_page(html, url, collect_media=True, clean=False)
    except Exception:  # noqa: BLE001
        data = {}
    # 原文本身是视频新闻页时，og:image 与首屏大图都是视频封面（台标 / 主播 / 片头卡），
    # 只有正文里嵌的图才可能是真实现场照片
    is_video_page = bool(data.get("videos")) or _has_og_video(html)
    urls = [] if is_video_page else [_abs(u, url) for u in _meta_images(html)]
    urls += list(data.get("images") or [])

    items: List[MediaItem] = []
    seen = set()
    for u in urls:
        if not u or u in seen or _is_bad_image(u):
            continue
        if looks_like_graphic(title, u):
            continue
        seen.add(u)
        items.append(MediaItem(
            kind="image", url=u, page_url=url, title=title,
            platform=platform or platforms.platform_of_url(url),
            provider="origin", keyword="",
            extra={"match": 1.0, "source_title": title},
        ))
        if len(items) >= limit:
            break
    return items


# ---------------------------------------------------------------- 检索编排
def _rank(items: List[MediaItem], exclude_platform: str, title: str = "",
          terms: List[str] = ()) -> List[MediaItem]:
    """跨平台过滤 + 排序：排除原平台 → 优先其它知名平台 → 标题相关度 → 大图优先。"""
    preferred = platforms.preferred_domains(exclude_platform)
    if title:
        _stamp(items, title, list(terms))

    def score(it: MediaItem) -> tuple:
        host = platforms.host_of(it.url or it.page_url)
        in_pref = any(host.endswith(d) for d in preferred)
        return (
            1 if platforms.is_same(it.platform, exclude_platform) else 0,  # 同平台排最后
            0 if in_pref else 1,                                          # 其它知名平台优先
            -it.extra.get("match", 0.0),                                  # 与原文标题越像越靠前
            0 if it.width >= 800 else 1,
        )

    cross = [it for it in items if not platforms.is_same(it.platform, exclude_platform)]
    # 同平台的只在素材不足时才允许使用（放末尾）
    cross += [it for it in items if platforms.is_same(it.platform, exclude_platform)]
    return sorted(cross, key=score)


async def search_images(client: httpx.AsyncClient, queries: List[str], limit: int,
                        exclude_platform: str = "", mode: str = "auto",
                        title: str = "", strict: bool = False) -> List[MediaItem]:
    """mode: targeted=只走搜索引擎；general=只走通用图库；auto=先引擎后图库。

    title=原文标题，用于给候选打相关分（以标题搜到的同源素材优先）。
    strict=True 表示这是关键词层（兜底）检索，相关度过滤更严。
    """
    found: List[MediaItem] = []
    seen = set()
    terms = keywords.title_terms(title) if title else []

    def absorb(items: List[MediaItem]) -> None:
        for it in items:
            if not it.url or it.url in seen:
                continue
            # 视频站页面里的图 = 视频封面 / 栏目片头；图注带片头台标字样的也不要
            if is_video_site(it.page_url or it.url):
                continue
            if looks_like_graphic(it.title or ""):
                continue
            seen.add(it.url)
            found.append(it)

    def enough() -> bool:
        return len(found) >= max(limit * 3, limit + 2)

    if mode in ("auto", "targeted"):
        # 1) 百度 / 必应图片检索（结果多是概念图，靠相关度排序压到后面）
        for provider in config.IMAGE_PROVIDERS:
            fn = IMAGE_PROVIDERS.get(provider)
            if not fn:
                continue
            for q in queries[:2]:
                absorb(await fn(client, q, limit))
                if enough():
                    break
            if enough():
                break
    if mode in ("auto", "general") and not enough():
        # 2) 引擎不足时用通用图库补齐（需 API Key）
        for provider in config.STOCK_PROVIDERS:
            fn = IMAGE_PROVIDERS.get(provider)
            if not fn:
                continue
            for q in queries:
                absorb(await fn(client, q, limit))
            if enough():
                break
    ranked = _rank(found, exclude_platform, title, terms)
    ranked = _keep_relevant(ranked, limit, strict)
    return ranked[: max(limit * 3, limit + 2)]


def video_passes(item: MediaItem, title: str, entities: List[str] = (),
                 strong: List[str] = ()) -> bool:
    """视频是否「确实在讲这件事」—— 两个视频通道共用同一套判定。

    要求相关度达标，且满足其一：拍到事件专名 / 命中 ≥2 个实词 /
    标题后半段与视频标题连续重合（撞的是事件而非同一个地名或同一位主角）。
    """
    floor = float(getattr(config, "MEDIA_VIDEO_MATCH_MIN", 0.0) or 0.0)
    if item.extra.get("match", 0.0) < floor:
        return False
    text = f"{item.title or ''} {item.page_url or ''}"
    if any(s and s in text for s in (strong or ())):
        return True
    hits = int(item.extra.get("hits") or 0)
    if not hits and entities:
        hits = sum(1 for e in entities if len(e) >= 2 and e in text)
    if hits >= int(getattr(config, "MEDIA_VIDEO_MIN_HITS", 2) or 0):
        return True
    return tail_lcs_len(title, item.title or "") >= int(
        getattr(config, "MEDIA_VIDEO_MIN_TAIL_LCS", 5) or 0)


async def search_videos(client: httpx.AsyncClient, queries: List[str], limit: int,
                        exclude_platform: str = "", title: str = "") -> List[MediaItem]:
    """视频检索：调用方只会传「完整标题」层的查询词。

    打分用 video_match（实体命中率为主），并按 config.MEDIA_VIDEO_MATCH_MIN 过滤：
    视频跑题的代价远高于缺视频（画面与标题不符直接不可用），宁缺勿滥。
    """
    found: List[MediaItem] = []
    seen = set()
    entities = keywords.title_entities(title) if title else []
    for provider in config.VIDEO_PROVIDERS:
        fn = VIDEO_PROVIDERS.get(provider)
        if not fn:
            continue
        for q in queries:
            for it in await fn(client, q, limit):
                key = it.page_url or it.url
                if key in seen:
                    continue
                seen.add(key)
                found.append(it)
        if len(found) >= limit * 3:
            break
    strong = keywords.strong_entities(title) if title else []
    for it in found:
        it.extra["match"] = video_match(it, title, entities, strong)
        it.extra["entities"] = list(entities)
        text = f"{it.title or ''} {it.page_url or ''}"
        it.extra["hits"] = sum(1 for e in entities if len(e) >= 2 and e in text)
    ranked = [it for it in found if not platforms.is_same(it.platform, exclude_platform)]
    if not ranked:
        ranked = found
    floor = float(getattr(config, "MEDIA_VIDEO_MATCH_MIN", 0.0) or 0.0)
    min_hits = int(getattr(config, "MEDIA_VIDEO_MIN_HITS", 2) or 0)
    min_tail = int(getattr(config, "MEDIA_VIDEO_MIN_TAIL_LCS", 4) or 0)
    # 复合条件：相关度达标，并且满足下列之一 —— ①提到事件专名；②命中 ≥2 个实词；
    # ③标题后半段（去掉开头的主体）与视频标题有连续重合。
    # 只凭整体字面相似度会把「同一主角、另一件事」的视频放进来（画面与标题不符）。
    base = [it for it in ranked if it.extra.get("match", 0.0) >= floor]

    def is_solid(it: MediaItem) -> bool:
        text = f"{it.title or ''} {it.page_url or ''}"
        return any(s and s in text for s in strong) or it.extra.get("hits", 0) >= min_hits

    # 从严到宽：有「专名 / 多实体命中」的确凿候选就只用它们，避免较松的条件占掉名额
    solid = [it for it in base if is_solid(it)]
    ranked = solid or [it for it in base if tail_lcs_len(title, it.title or "") >= min_tail]
    # 相关度优先；其次偏好时长合理的片段（太短不可用，太长会被截断却有片头）
    ranked.sort(key=lambda x: (-x.extra.get("match", 0.0),
                               0 if 8 <= x.duration <= 600 else 1,
                               -x.duration))
    return ranked[: max(limit * 3, limit + 2)]


async def search_news_videos(client: httpx.AsyncClient, title: str, queries: List[str],
                             limit: int, exclude_platform: str = "") -> List[MediaItem]:
    """同源报道自带的视频：这场同名事件其它报道页里嵌的新闻短片。

    比搜索引擎结果靠谱得多 —— 是媒体自己发的片子，天然「视频 = 这件事」。
    走不到 Workable 直链的（多数是播放器 iframe）直接丢弃。
    """
    if not title:
        return []
    entities = keywords.title_entities(title)
    strong = keywords.strong_entities(title)
    pages = await _news_pages(client, (queries or [title])[0], max(limit, 3))
    out: List[MediaItem] = []
    for p in pages[:4]:
        if platforms.is_same(p.platform, exclude_platform):
            continue
        for v in await _page_videos(client, p.url, p.title, title, entities):
            # 报道页里的视频同样要过相关度：有些页面挂的是同栏目其它新闻的片子
            if platforms.is_same(v.platform, exclude_platform):
                continue
            if not video_passes(v, title, entities, strong):
                continue
            out.append(v)
            if len(out) >= limit * 2:
                return out
    return out


_VID_EXT = (".mp4", ".m3u8", ".webm", ".mov")


def _is_video_url(url: str) -> bool:
    low = (url or "").lower()
    if any(x in low for x in ("poster", "logo", ".gif", ".jpg", ".png", ".webp")):
        return False
    return low.endswith(_VID_EXT) or "/m3u8" in low or ".mp4?" in low


async def _page_videos(client: httpx.AsyncClient, url: str, page_title: str,
                       title: str, entities: List[str]) -> List[MediaItem]:
    """从报道页里取视频直链（<video src> / og:video / 播放器 iframe）。"""
    html = await _fetch_html(client, url)
    if not html:
        return []
    urls: List[str] = []
    for tag in _META_TAG.findall(html):
        m = re.search(r'(?:property|name)=["\']([^"\']+)["\']', tag, re.I)
        if (m and "video" in m.group(1).lower()):
            mc = re.search(r'content=["\']([^"\']+)["\']', tag)
            if mc:
                urls.append(mc.group(1))
    try:
        data = extract.parse_page(html, url, collect_media=True, clean=False)
    except Exception:  # noqa: BLE001
        data = {}
    urls += list(data.get("videos") or [])

    out: List[MediaItem] = []
    seen = set()
    for u in urls:
        abs_u = _abs(u, url)
        if not abs_u or abs_u in seen or not _is_video_url(abs_u):
            continue
        seen.add(abs_u)
        item = MediaItem(
            kind="video", url=abs_u, page_url=url, title=page_title,
            platform=platforms.platform_of_url(url), provider="news", keyword="",
        )
        item.extra["match"] = video_match(item, title, entities)
        item.extra["source_title"] = page_title
        out.append(item)
    return out
