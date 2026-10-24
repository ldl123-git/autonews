"""素材下载：图片直链 / B 站视频地址解析。"""
import re
from typing import Dict, Optional, Tuple

import httpx

from .. import config
from .search import _headers

IMAGE_MIME = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/avif")

# 部分站点的图片 CDN（如搜狐 itc.cn）只放行移动端 / 无 UA 的请求，
# 桌面 UA 会被防盗链直接 403，因此下载时按顺序轮换 UA 重试。
_IMAGE_UAS = (config.USER_AGENT, config.MOBILE_UA, "")


async def fetch_image(client: httpx.AsyncClient, url: str, referer: str = "") -> Optional[bytes]:
    """下载图片字节，带 Referer 与体积校验。

    不少站点（搜狐 / 网易等）有防盗链，没有 Referer 会直接 403，
    因此默认回退到图片自身域名，调用方应尽量传来源页 URL。
    仍失败时轮换 UA 再试，避免单个 CDN 的 UA 策略把整篇素材卡死。
    """
    if not url:
        return None
    if referer and not referer.startswith("http"):
        referer = ""          # 来源页地址不合法（如百度加密串没解开）时按无 Referer 处理
    if not referer:
        if "bilibili" in url:
            referer = "https://www.bilibili.com/"
        else:
            m = re.match(r"(https?://[^/]+)", url)
            referer = (m.group(1) + "/") if m else ""
    for ua in _IMAGE_UAS:
        headers = _headers(referer)
        if ua:
            headers["User-Agent"] = ua
        else:
            headers.pop("User-Agent", None)
        data = await _fetch_image_once(client, url, headers)
        if data:
            return data
    return None


async def fetch_image_any(client: httpx.AsyncClient, urls, referer: str = "") -> Optional[bytes]:
    """依次尝试多个直链，取第一个成功的。

    百度图片一个结果会同时给出 objURL / hoverURL / middleURL，主链过期时要能回退。
    """
    for url in urls:
        if not url:
            continue
        data = await fetch_image(client, url, referer)
        if data:
            return data
    return None


async def _fetch_image_once(client: httpx.AsyncClient, url: str,
                            headers: Dict[str, str]) -> Optional[bytes]:
    try:
        async with client.stream("GET", url, headers=headers,
                                 timeout=config.MEDIA_TIMEOUT, follow_redirects=True) as resp:
            if resp.status_code >= 400:
                return None
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype and not ctype.startswith("image/"):
                return None
            data = b""
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > config.MEDIA_MAX_BYTES:
                    break
    except Exception:  # noqa: BLE001
        return None
    return data or None


async def resolve_bilibili(client: httpx.AsyncClient, bvid: str) -> Tuple[str, Dict[str, str]]:
    """BVID -> 可下载地址 + ffmpeg 需要的请求头。"""
    headers = _headers("https://www.bilibili.com/")
    try:
        view = (await client.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid}, headers=headers, timeout=config.MEDIA_TIMEOUT,
        )).json()
        cid = ((view.get("data") or {}).get("cid")) or 0
        if not cid:
            return "", {}
        play = (await client.get(
            "https://api.bilibili.com/x/player/playurl",
            params={"bvid": bvid, "cid": cid, "qn": 16, "fnval": 0, "otype": "json"},
            headers=headers, timeout=config.MEDIA_TIMEOUT,
        )).json()
        durl = ((play.get("data") or {}).get("durl") or [])
        if not durl:
            return "", {}
        return durl[0].get("url") or "", headers
    except Exception:  # noqa: BLE001
        return "", {}


def ffmpeg_headers(headers: Dict[str, str]) -> str:
    """把请求头拼成 ffmpeg -headers 参数（CRLF 分隔）。"""
    if not headers:
        return ""
    parts = [f"{k}: {v}" for k, v in headers.items() if k.lower() in ("referer", "user-agent", "cookie")]
    return "\r\n".join(parts) + "\r\n"


async def probe_url_ok(client: httpx.AsyncClient, url: str) -> bool:
    try:
        resp = await client.head(url, headers=_headers(), timeout=10, follow_redirects=True)
        return resp.status_code < 400
    except Exception:  # noqa: BLE001
        return False
