"""今日头条链接适配：优先走移动端 info 接口取结构化正文。

为什么不能只解析网页：头条（www/m.toutiao.com）现在返回的是字节跳动反爬
JSVM 壳（`<body></body>` + `_$jsvmprt` 脚本），正文完全不在 HTML 里，桌面 UA
和移动 UA 都一样，HTMLParser 只能解析出 0 字。

而 `https://m.toutiao.com/i{gid}/info/` 直接返回结构化 JSON（无需 cookie / 签名），
三种形态都能覆盖：

* 图文     data.title / data.content（HTML）/ data.publish_time
* 微头条   data.thread.thread_base.content（纯文本）/ title / large_image_list
* 视频     data.title / data.content（简介）/ poster_url / group_source=19

本模块只做「拿到 data → 归一成 extract.parse_page 同构字典」，抓取与写库仍由
fetcher 负责，因此素材 / 导出 / 改写链路零改动。
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from . import extract
from .config import MOBILE_UA

log = logging.getLogger(__name__)

INFO_URL = "https://m.toutiao.com/i{gid}/info/"

_HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://m.toutiao.com/",
}

# 头条链接里的内容 ID：/item/、/article/、/video/、/group/、/w/、/a{id}、/i{id}/info
_GID_PATTERNS = (
    re.compile(r"/(?:item|article|video|group|w)/(\d{6,})"),
    re.compile(r"/a(\d{6,})"),
    re.compile(r"/i(\d{6,})/info"),
)

# 微头条 title 往往就是整段正文，截第一行且限长，别把全文灌进列表标题
_TITLE_MAX = 80


def is_toutiao(url: str) -> bool:
    return "toutiao.com" in (url or "").lower()


def gid_from_url(url: str) -> str:
    """从链接里取内容 ID，取不到返回空串。"""
    for rgx in _GID_PATTERNS:
        m = rgx.search(url or "")
        if m:
            return m.group(1)
    return ""


async def resolve_gid(client: httpx.AsyncClient, url: str) -> str:
    """短链（m.toutiao.com/is/xxx/）先跟一次重定向，再从最终地址里取 ID。"""
    gid = gid_from_url(url)
    if gid:
        return gid
    try:
        resp = await client.get(url, headers=_HEADERS)
        return gid_from_url(str(resp.url))
    except Exception as exc:  # noqa: BLE001 - 取不到就走原有 HTML 回退
        log.warning("头条短链解析失败 %s：%s", url, exc.__class__.__name__)
        return ""


async def fetch_detail(client: httpx.AsyncClient, gid: str) -> Optional[Dict[str, Any]]:
    """请求 info 接口，返回 JSON 的 data 字段；失败返回 None（由调用方回退）。"""
    url = INFO_URL.format(gid=gid)
    try:
        resp = await client.get(url, headers=_HEADERS)
    except httpx.TimeoutException:
        log.warning("头条 info 接口超时 gid=%s", gid)
        return None
    except httpx.HTTPError as exc:
        log.warning("头条 info 接口网络错误 gid=%s：%s", gid, exc.__class__.__name__)
        return None
    if resp.status_code >= 400:
        log.warning("头条 info 接口 HTTP %s gid=%s", resp.status_code, gid)
        return None
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError):
        log.warning("头条 info 接口返回非 JSON gid=%s", gid)
        return None
    data = (payload or {}).get("data")
    if not isinstance(data, dict) or not data:
        log.warning("头条 info 接口无 data gid=%s", gid)
        return None
    return data


def to_page_data(detail: Dict[str, Any], url: str, collect_media: bool = True) -> Optional[Dict[str, Any]]:
    """把 info 接口的 data 归一成 parse_page 的同构字典；没有有效内容返回 None。"""
    thread = ((detail.get("thread") or {}).get("thread_base") or {})
    biz = str(detail.get("biz_tag") or "")
    group_source = str(detail.get("group_source") or "")
    is_video = bool(detail.get("video_id") or thread.get("video_group")) \
        or group_source == "19" or biz == "视频" or "video" in url

    html = detail.get("content") or thread.get("content") or ""
    page = extract.parse_page(_wrap(html), url, collect_media=collect_media)

    images = _thread_images(thread)
    if collect_media and images:
        page["images"] = _dedup(images + list(page.get("images") or []))[:40]
    page["img_count"] = max(len(images), int(page.get("img_count") or 0))

    if is_video:
        poster = str(detail.get("poster_url") or "").strip()
        if collect_media and poster:
            page["images"] = _dedup([poster] + list(page.get("images") or []))[:40]
            page["img_count"] = max(1, page["img_count"])
        page["video_count"] = max(1, int(page.get("video_count") or 0))

    title = _clean_title(detail.get("title") or thread.get("title") or "", page.get("text") or "")
    if not title:
        title = (page.get("text") or "").split("\n")[0][:60]
    page["title"] = title

    meta = dict(page.get("meta") or {})
    meta.update({"og:site_name": "今日头条", "og:title": title})
    if is_video:
        meta["og:type"] = "video"
    page["meta"] = meta

    plain = re.sub(r"\s+", "", page.get("text") or "")
    # 视频只有简介、图片型内容没有文字，这两种只要有标题/图片就算抓取成功
    if len(plain) < 30 and not (is_video and title) and not page.get("img_count"):
        log.warning("头条 info 正文过短（%s 字）gid=%s", len(plain), gid_from_url(url))
        return None
    return page


def _wrap(content: str) -> str:
    """把 info 返回的正文包进 <article>。

    必须包：图文 content 是若干并列的 <p>/<div>，没有外层容器时 HTMLParser 会把
    每个顶层节点算成独立候选，`_pick_main_text` 只取最长的一段（实测 20 段只剩
    337 字）；包一层后所有段落聚合到同一容器。
    """
    s = (content or "").strip()
    if not s:
        return ""
    if "<" not in s:
        # 微头条 content 是纯文本，按行转段落；HTMLParser 空栈会丢弃裸文本
        lines = [x.strip() for x in s.splitlines()]
        s = "".join(f"<p>{line}</p>" for line in lines if line)
    return f"<article>{s}</article>"


def _clean_title(raw: str, text: str) -> str:
    title = str(raw or "").strip()
    if not title:
        return ""
    title = title.splitlines()[0].strip()
    # 微头条常常「标题 == 正文」，这种标题用正文首句，避免整段灌进列表
    if text and title and text.replace("\n", "").startswith(title.replace("\n", "")[:20]) and len(title) > 40:
        title = title[:40].rstrip("，,。.、；; ") + "…"
    return title[:_TITLE_MAX].strip()


def _thread_images(thread: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for key in ("large_image_list", "origin_image_list", "thumb_image_list", "ugc_cut_image_list"):
        for item in thread.get(key) or []:
            if not isinstance(item, dict):
                continue
            src = str(item.get("url") or "").strip()
            if not src:
                urls = item.get("url_list") or []
                if urls and isinstance(urls[0], dict):
                    src = str(urls[0].get("url") or "").strip()
            if src:
                out.append(src)
    return out


def _dedup(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
