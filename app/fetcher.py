"""原文抓取引擎：异步队列 + 并发抓取 + 结果写入临时缓存区。"""
import asyncio
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
import httpx

from . import extract
from . import exporter
from .config import (
    AUTO_EXPORT,
    CACHE_DIR,
    COLLECT_MEDIA,
    FETCH_CONCURRENCY,
    FETCH_RETRY,
    FETCH_TIMEOUT,
    MOBILE_UA,
    USER_AGENT,
)
from .db import get_conn, write_conn

SAVE_HTML = False  # 是否同时保存原始 HTML（默认关闭，避免磁盘膨胀）


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class FetchEngine:
    def __init__(self, concurrency: int = FETCH_CONCURRENCY, timeout: float = FETCH_TIMEOUT,
                 retry: int = FETCH_RETRY) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.concurrency = concurrency
        self.timeout = timeout
        self.retry = retry
        self.running = 0
        self._workers: List[asyncio.Task] = []

    async def start(self) -> None:
        if self._workers:
            return
        for i in range(self.concurrency):
            self._workers.append(asyncio.create_task(self._worker(i)))

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        self._workers.clear()

    def enqueue(self, article_ids: List[int]) -> int:
        ids = [int(i) for i in article_ids if i]
        if not ids:
            return 0
        for aid in ids:
            self.queue.put_nowait(aid)
        with write_conn() as conn:
            conn.executemany(
                "UPDATE articles SET fetch_status='queued', fetch_error='' WHERE id=?",
                [(aid,) for aid in ids],
            )
        return len(ids)

    def stats(self) -> Dict[str, int]:
        return {"queued": self.queue.qsize(), "running": self.running}

    # ---------------- worker ----------------
    async def _worker(self, idx: int) -> None:
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.timeout,
            headers=headers,
            limits=httpx.Limits(max_connections=self.concurrency, max_keepalive_connections=self.concurrency),
        ) as client:
            while True:
                article_id = await self.queue.get()
                self.running += 1
                try:
                    await self._fetch_one(client, article_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._mark_failed(article_id, f"抓取异常：{exc}")
                finally:
                    self.running -= 1
                    self.queue.task_done()

    async def _fetch_one(self, client: httpx.AsyncClient, article_id: int) -> None:
        article = self._get_article(article_id)
        if not article:
            return
        url = (article["url"] or "").strip()
        if not url:
            self._mark_failed(article_id, "缺少原文链接")
            return
        with write_conn() as conn:
            conn.execute(
                "UPDATE articles SET fetch_status='fetching', fetch_error='' WHERE id=?", (article_id,)
            )

        last_error = ""
        for attempt in range(self.retry + 1):
            try:
                req_headers = {"User-Agent": MOBILE_UA if attempt else USER_AGENT,
                               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                               "Accept-Language": "zh-CN,zh;q=0.9",
                               "Referer": _root(url)}
                resp = await client.get(url, headers=req_headers)
                if resp.status_code >= 400:
                    last_error = f"HTTP {resp.status_code}"
                    continue
                raw = resp.content
                if not raw:
                    last_error = "响应内容为空"
                    continue
                enc = extract.detect_encoding(raw[:4096], resp.headers.get("content-type", ""))
                try:
                    html = raw.decode(enc, errors="replace")
                except LookupError:
                    html = raw.decode("utf-8", errors="replace")
                data = extract.parse_page(html, url, collect_media=COLLECT_MEDIA)
                text = data["text"]
                if len(re.sub(r"\s+", "", text)) < 30:
                    last_error = "未提取到有效正文"
                    continue
                content_type = extract.classify(
                    article["content_type"] or "", text,
                    data.get("img_count", 0), data.get("video_count", 0), data["meta"],
                )
                await self._save(article, data, content_type, url, len(html), html)
                return
            except httpx.TimeoutException:
                last_error = "请求超时"
            except httpx.HTTPError as exc:
                last_error = f"网络错误：{exc.__class__.__name__}"
            except Exception as exc:  # noqa: BLE001
                last_error = f"解析失败：{exc}"
        self._mark_failed(article_id, last_error or "抓取失败")

    # ---------------- persistence ----------------
    @staticmethod
    def _get_article(article_id: int) -> Optional[Dict]:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _mark_failed(article_id: int, error: str) -> None:
        with write_conn() as conn:
            conn.execute(
                "UPDATE articles SET fetch_status='failed', fetch_error=?, updated_at=? WHERE id=?",
                (error[:300], _now(), article_id),
            )

    async def _save(self, article: Dict, data: Dict, content_type: str, url: str,
                    html_len: int, html: str) -> None:
        article_id = article["id"]
        now = _now()
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        payload = {
            "article_id": article_id,
            "url": url,
            "title": data.get("title") or article["title"],
            "content_type": content_type,
            "text": data.get("text", ""),
            "images": data.get("images", []),
            "videos": data.get("videos", []),
            "meta": {k: v for k, v in (data.get("meta") or {}).items()
                     if k in ("og:title", "og:type", "description", "keywords", "og:site_name")},
            "source": article.get("source", ""),
            "category": article.get("category", ""),
            "publish_time": article.get("publish_time", ""),
            "read_count": article.get("read_count", 0),
            "like_count": article.get("like_count", 0),
            "fetched_at": now,
        }
        path = CACHE_DIR / f"{article_id}-{stamp}.json"
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(payload, ensure_ascii=False, indent=2))

        if SAVE_HTML:
            async with aiofiles.open(path.with_suffix(".html"), "w", encoding="utf-8") as f:
                await f.write(html)

        with write_conn() as conn:
            old = conn.execute("SELECT path FROM raw_cache WHERE article_id=?", (article_id,)).fetchone()
            if old:
                _remove_file(old["path"])
                conn.execute("DELETE FROM raw_cache WHERE article_id=?", (article_id,))
            cur = conn.execute(
                """INSERT INTO raw_cache
                   (article_id, url, title, content_type, summary, text_len, image_count,
                    video_count, images, videos, path, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    article_id, url, payload["title"], content_type,
                    extract.summarize(data.get("text", "")),
                    len(data.get("text", "")),
                    len(data.get("images", [])),
                    len(data.get("videos", [])),
                    json.dumps(data.get("images", []), ensure_ascii=False),
                    json.dumps(data.get("videos", []), ensure_ascii=False),
                    str(path), now,
                ),
            )
            content_id = int(cur.lastrowid)
            conn.execute(
                """UPDATE articles SET fetch_status='success', fetch_error='', content_id=?,
                   content_type=?, fetched_at=?, updated_at=? WHERE id=?""",
                (content_id, content_type, now, now, article_id),
            )

        if AUTO_EXPORT:
            # 导出到 output/<标题>/（同步 IO，放线程池，避免阻塞抓取队列）
            await asyncio.to_thread(exporter.export_article, article_id)


def _remove_file(path: Optional[str]) -> None:
    if not path:
        return
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
    except OSError:
        pass


def _root(url: str) -> str:
    m = re.match(r"(https?://[^/]+)", url or "")
    return m.group(1) + "/" if m else "https://www.baidu.com/"


engine = FetchEngine()


async def fetch_one_now(article_id: int, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    """在任意 async 上下文里直接抓取一篇文章，写库后返回最新状态。

    素材收集在「原文未抓取」时复用本函数，先抓原文再继续，使素材与原文
    抓取可以不分先后、独立入队。
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            follow_redirects=True, timeout=FETCH_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )
    inst = FetchEngine(concurrency=1, timeout=FETCH_TIMEOUT, retry=FETCH_RETRY)
    try:
        await inst._fetch_one(client, article_id)
    finally:
        if owns_client:
            await client.aclose()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT fetch_status, fetch_error, content_id FROM articles WHERE id=?",
            (article_id,),
        ).fetchone()
    return dict(row) if row else {}
