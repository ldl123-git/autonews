"""图片搜索引擎解析 —— 参照 icrawler（https://github.com/hellock/icrawler）。

本地参考实现位于 app/project/icrawler-master，本模块逐项对齐了它的做法：

===========================  ==========================================================
icrawler 位置                 搬过来的东西
===========================  ==========================================================
builtin/baidu.py             acjson 分页地址 + ``BaiduParser._decode_url`` 的 objURL 解密表
builtin/bing.py              ``/images/async`` 分页地址 + ``m`` 属性里的 murl 字典解析
builtin/filter.py            size / type / layout / license 过滤串（必应 ``qft=``、百度 ``z=``）
downloader.py                ``ImageDownloader.keep_file`` 的像素尺寸取舍思路
===========================  ==========================================================

没有直接 pip 安装 icrawler 的原因：
1. 它是 requests + 线程池的同步爬虫，本项目是 httpx 异步流水线，套一层线程会把
   FastAPI 的事件循环堵住；
2. 它只按自增序号落盘（000001.jpg），不保留来源页 / 平台 / 原始标题，
   而本项目的素材清单必须能溯源（相关度打分、跨平台取材都依赖这些字段）。

因此这里只搬「检索与过滤」的算法，下载与加工仍走 download.py / process.py。
"""
import html as _html
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from .. import config
from . import platforms

# 必应结果页：m="{&quot;murl&quot;:&quot;https://...&quot;,&quot;purl&quot;:...}"
_MBLOCK = re.compile(r"""m=['"](\{.*?\})['"]""")
_MURL = re.compile(r'"murl":"(.*?)"')
_PURL = re.compile(r'"purl":"(.*?)"')
_TITLE = re.compile(r'"t":"(.*?)"')
_TAG = re.compile(r"<[^>]+>")

# ---------------------------------------------------------------- 过滤串
# 与 icrawler/builtin/bing.py 的 BingFeeder.get_filter 一一对应
_BING_LICENSE = {
    "creativecommons": "licenseType-Any",
    "publicdomain": "license-L1",
    "noncommercial": "license-L2_L3_L4_L5_L6_L7",
    "commercial": "license-L2_L3_L4",
    "noncommercial,modify": "license-L2_L3_L5_L6",
    "commercial,modify": "license-L2_L3",
}

# 与 icrawler/builtin/baidu.py 的 BaiduFeeder.get_filter 一一对应
_BAIDU_SIZE = {"extralarge": 9, "large": 3, "medium": 2, "small": 1}
_BAIDU_TYPE = {
    "portrait": "s=3&lm=0&st=-1&face=0",
    "face": "s=0&lm=0&st=-1&face=1",
    "clipart": "s=0&lm=0&st=1&face=0",
    "linedrawing": "s=0&lm=0&st=2&face=0",
    "animated": "s=0&lm=6&st=-1&face=0",
    "static": "s=0&lm=7&st=-1&face=0",
}


def bing_filters(filters: Optional[Dict[str, str]] = None) -> str:
    """把通用过滤条件翻译成必应的 qft 串（icrawler 里 sep='' 直接拼接）。"""
    opts = config.IMAGE_FILTERS if filters is None else (filters or {})
    parts: List[str] = []
    for key, val in opts.items():
        if not val:
            continue
        if key == "size":
            parts.append("+filterui:imagesize-wallpaper" if val == "extralarge"
                         else f"+filterui:imagesize-{val}")
        elif key == "type":
            parts.append("+filterui:photo-" + ("animatedgif" if val == "animated" else val))
        elif key == "layout":
            parts.append(f"+filterui:aspect-{val}")
        elif key == "license" and val in _BING_LICENSE:
            parts.append("+filterui:" + _BING_LICENSE[val])
    return "".join(parts)


def baidu_filters(filters: Optional[Dict[str, str]] = None) -> str:
    """把通用过滤条件翻译成百度的查询串。"""
    opts = config.IMAGE_FILTERS if filters is None else (filters or {})
    parts: List[str] = []
    for key, val in opts.items():
        if not val:
            continue
        if key == "size" and val in _BAIDU_SIZE:
            parts.append(f"z={_BAIDU_SIZE[val]}")
        elif key == "type" and val in _BAIDU_TYPE:
            parts.append(_BAIDU_TYPE[val])
    return "&".join(parts)


# ---------------------------------------------------------------- 百度 objURL
# 百度返回的 objURL / hoverURL 是替换过的密文，必须按两张映射表还原（见 icrawler）。
_MAP1 = {"_z2C$q": ":", "_z&e3B": ".", "AzdH3F": "/"}
_MAP2 = {
    "w": "a", "k": "b", "v": "c", "1": "d", "j": "e", "u": "f", "2": "g",
    "i": "h", "t": "i", "3": "j", "h": "k", "s": "l", "4": "m",
    "g": "n", "5": "o", "r": "p", "q": "q", "6": "r", "f": "s",
    "p": "t", "7": "u", "e": "v", "o": "w", "8": "1", "d": "2",
    "n": "3", "9": "4", "c": "5", "m": "6", "0": "7", "b": "8",
    "l": "9", "a": "0",
}  # yapf: disable


def decode_baidu_url(url: str) -> str:
    """解密百度的 objURL / hoverURL（照搬 icrawler BaiduParser._decode_url）。"""
    if not url:
        return ""
    for cipher, plain in _MAP1.items():
        url = url.replace(cipher, plain)
    return "".join(_MAP2.get(ch, ch) for ch in url)


def _search():
    """延迟导入 search，避免与它形成循环依赖（search 在模块加载期就 import 本模块）。"""
    from . import search
    return search


def _strip_tags(s: str) -> str:
    return _TAG.sub("", _html.unescape(s or "")).strip()


# ---------------------------------------------------------------- 百度图片
async def baidu_images(client: httpx.AsyncClient, query: str, limit: int,
                       filters: Optional[Dict[str, str]] = None) -> List[Any]:
    """百度图片检索：acjson 接口，一次 30 条，取到够为止。"""
    MediaItem = _search().MediaItem
    headers = _search()._headers("https://image.baidu.com/")
    extra = baidu_filters(filters)
    need = max(limit * 3, 24)
    items: List[Any] = []
    for pn in range(0, need + 30, 30):
        url = ("https://image.baidu.com/search/acjson?tn=resultjson_com"
               f"&ipn=rj&word={quote(query)}&pn={pn}&rn=30")
        if extra:
            url += "&" + extra
        try:
            resp = await client.get(url, headers=headers, timeout=config.MEDIA_TIMEOUT,
                                    follow_redirects=True)
            if resp.status_code >= 400:
                break
            data = json.loads(resp.text.replace("\\'", "'"), strict=False)
        except Exception:  # noqa: BLE001
            break
        rows = data.get("data") or []
        if not rows:
            break
        for r in rows:
            if not isinstance(r, dict):
                continue
            urls: List[str] = []
            for key in ("objURL", "hoverURL", "middleURL", "thumbURL"):
                u = decode_baidu_url(str(r.get(key) or ""))
                if u.startswith("http") and u not in urls:
                    urls.append(u)
            if not urls:
                continue
            # fromURL 与 objURL 同一套加密，解出来才是真实的报道页地址
            page = decode_baidu_url(str(r.get("fromURL") or ""))
            if not page.startswith("http"):
                page = ""
            title = _strip_tags(str(r.get("fromPageTitleEnc") or ""))
            items.append(MediaItem(
                kind="image", url=urls[0], page_url=page, title=title,
                platform=platforms.platform_of_url(page or urls[0]),
                provider="baidu", keyword=query,
                width=int(r.get("width") or 0), height=int(r.get("height") or 0),
                extra={"alt_urls": urls[1:]},
            ))
        if len(items) >= need:
            break
    return items


# ---------------------------------------------------------------- 必应图片
async def bing_images(client: httpx.AsyncClient, query: str, limit: int,
                      filters: Optional[Dict[str, str]] = None) -> List[Any]:
    """必应图片检索：/images/async 片段页，m 属性里带 murl / purl / 标题 / 尺寸。"""
    MediaItem = _search().MediaItem
    headers = _search()._headers("https://www.bing.com/")
    qft = bing_filters(filters)
    url = (f"https://www.bing.com/images/async?q={quote(query)}"
           f"&first=0&count={max(limit * 3, 35)}&mmasync=1")
    if qft:
        url += "&qft=" + qft
    try:
        # 必应会 302 到带检索会话的地址，不跟随重定向拿不到结果
        resp = await client.get(url, headers=headers, timeout=config.MEDIA_TIMEOUT,
                                follow_redirects=True)
        if resp.status_code >= 400:
            return []
        page = _html.unescape(resp.text)
    except Exception:  # noqa: BLE001
        return []

    items: List[Any] = []
    for raw in _MBLOCK.findall(page):
        try:
            d = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        src = str(d.get("murl") or "")
        if not src.startswith("http"):
            continue
        try:
            w, h = int(d.get("w") or 0), int(d.get("h") or 0)
        except (TypeError, ValueError):
            w, h = 0, 0
        items.append(MediaItem(
            kind="image", url=src, page_url=str(d.get("purl") or ""),
            title=_strip_tags(str(d.get("t") or "")),
            platform=platforms.platform_of_host(platforms.host_of(src)),
            provider="bing", keyword=query, width=w, height=h,
        ))
    if not items:  # 结构变了就退回裸正则
        purls = _PURL.findall(page)
        for i, src in enumerate(_MURL.findall(page)):
            if not src.startswith("http"):
                continue
            items.append(MediaItem(
                kind="image", url=src, page_url=purls[i] if i < len(purls) else "",
                platform=platforms.platform_of_host(platforms.host_of(src)),
                provider="bing", keyword=query,
            ))
    return items


IMAGE_ENGINES = {
    "baidu": baidu_images,
    "bing": bing_images,
}
