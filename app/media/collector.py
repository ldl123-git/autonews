"""素材收集编排：关键词 → 跨平台检索 → 下载 → 二次加工 → 存入 output/<标题>/素材/。"""
import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .. import config, exporter
from ..db import get_conn, write_conn
from ..fetcher import fetch_one_now
from . import download, keywords, platforms, process, search

IMG_DIRNAME = "素材/图片"
VID_DIRNAME = "素材/视频"
FRAME_DIRNAME = "素材/视频抽帧"
MANIFEST = "素材/素材清单.json"


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class MediaEngine:
    def __init__(self, concurrency: int = config.MEDIA_CONCURRENCY) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.concurrency = max(1, concurrency)
        self.running = 0
        self._workers: List[asyncio.Task] = []

    async def start(self) -> None:
        if self._workers:
            return
        for _ in range(self.concurrency):
            self._workers.append(asyncio.create_task(self._worker()))

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        self._workers.clear()

    def enqueue(self, article_ids: List[int], opts: Optional[Dict[str, Any]] = None) -> int:
        ids = [int(i) for i in article_ids if i]
        if not ids:
            return 0
        for aid in ids:
            self.queue.put_nowait({"id": aid, "opts": opts or {}})
        with write_conn() as conn:
            conn.executemany(
                "UPDATE articles SET media_status='queued', media_error='' WHERE id=?",
                [(aid,) for aid in ids],
            )
        return len(ids)

    def stats(self) -> Dict[str, int]:
        return {"queued": self.queue.qsize(), "running": self.running}

    # ---------------- worker ----------------
    async def _worker(self) -> None:
        async with httpx.AsyncClient(timeout=config.MEDIA_TIMEOUT,
                                     limits=httpx.Limits(max_connections=8)) as client:
            while True:
                job = await self.queue.get()
                article_id = job["id"]
                self.running += 1
                try:
                    await self._collect_one(client, article_id, job.get("opts") or {})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._mark(article_id, "failed", f"收集异常：{exc}")
                finally:
                    self.running -= 1
                    self.queue.task_done()

    # ---------------- 主流程 ----------------
    async def _collect_one(self, client: httpx.AsyncClient, article_id: int,
                           opts: Optional[Dict[str, Any]] = None) -> None:
        opts = opts or {}
        article = _load_article(article_id)
        if not article:
            return
        self._mark(article_id, "running", "")
        text = _article_text(article)
        # 原文未抓取：先抓原文再收集，使「抓取素材」可与「抓取原文」独立入队、不分先后
        if not text:
            try:
                info = await fetch_one_now(article_id)
            except Exception as exc:  # noqa: BLE001
                self._mark(article_id, "failed", f"抓取原文失败：{exc}")
                return
            if info.get("fetch_status") != "success":
                self._mark(article_id, "failed",
                           f"抓取原文未成功：{info.get('fetch_error') or '未知原因'}")
                return
            article = _load_article(article_id) or {}
            text = _article_text(article)
            if not text:
                self._mark(article_id, "failed", "已抓取原文但仍取不到正文")
                return

        directory = exporter.article_dir(article)
        if not (directory / "原文.txt").exists():
            exporter.export_article(article_id)

        title = article.get("title") or ""
        origin = platforms.detect(article.get("url") or "", article.get("source") or "")
        # 重采前清掉旧素材：否则重复点击「抓取素材」会让记录与文件不断累加
        clear_assets(article_id)
        # 检索词分层：标题层 = 完整标题（不分词），关键词层 = 正文关键词兜底
        title_qs, keyword_qs = keywords.build_query_tiers(
            title, text, article.get("category") or "", limit=3,
            custom=opts.get("keywords") or [])
        title_qs = title_qs or ([title] if title else [])
        # 0 / None 一律表示「用服务端默认配置」：前端各入口传的就是 0，
        # 若按字面理解为「不采」，视频通道会被永久跳过（曾导致所有文章都没视频）。
        want_img = max(0, int(opts.get("image_count") or config.MEDIA_IMAGE_COUNT))
        want_vid = max(0, int(opts.get("video_count") or config.MEDIA_VIDEO_COUNT))
        min_img = (min(want_img, max(1, int(getattr(config, "MEDIA_IMAGE_MIN", 0) or 0)))
                   if want_img else 0)

        images, img_note = await self._collect_images(client, directory, title_qs, keyword_qs,
                                                      origin, want_img, article_id, title,
                                                      article.get("url") or "")
        videos, vid_note = await self._collect_videos(client, directory, title_qs, origin,
                                                      want_vid, article_id, title)
        note = "；".join(x for x in (vid_note, img_note) if x)

        manifest = {
            "article_id": article_id,
            "title": article.get("title") or "",
            "origin_platform": origin,
            "title_query": title_qs[0] if title_qs else "",
            "keywords": {"title": title_qs, "fallback": keyword_qs},
            "images": images,
            "videos": videos,
            "notes": {"images": img_note, "videos": vid_note},
            "collected_at": _now(),
            "options": {
                "image_count": want_img,
                "image_min": min_img,
                "video_count": want_vid,
                "size": list(config.IMG_TARGET),
                "format": config.IMG_FORMAT,
                "clip_seconds": config.VIDEO_CLIP_SECONDS,
                "dew_band": config.IMG_DEWATERMARK,   # 是否裁掉了边缘文字条（水印 / 台标 / 字幕）
                "watermark": bool(config.IMG_WATERMARK),
            },
        }
        (directory / MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        # 素材命中的判定：只要图片或视频有一个就算采集成功。
        # 「没采到视频」「图片少于目标数」都是按标题检索的正常结果，不是异常，
        # 只能写成提示；一条都没匹配上才算 empty（无素材），同样不算失败，
        # 后续改写 / 交付照常走文字稿。
        if images or videos:
            status = "success"
            err = f"提示：{note}" if note else ""
        else:
            status = "empty"
            err = "无匹配素材" + (f"：{note}" if note else "（按标题检索未命中，正常跳过）")
        self._mark(article_id, status, err, count=len(images) + len(videos))
        # 素材就绪后重跑一次交付：先前的文字版会补上图片/视频（幂等覆盖）。
        # 只有已经产出合格改写稿时才触发 —— 还没改写的文章去交付只会得到
        # 「改写稿正文为空」，把 finish_status 写成 failed，白白落进异常列。
        if (images or videos) and _has_rewrite(article_id):
            try:
                from .. import finish
                await asyncio.to_thread(finish.export_article, article_id)
            except Exception:  # noqa: BLE001 - 交付失败不影响采集结果
                pass

    async def _collect_images(self, client, directory: Path, title_qs: List[str],
                              keyword_qs: List[str], origin: str, want: int,
                              article_id: int, title: str = "", url: str = ""):
        """按「标题优先、关键词兜底」两层检索图片。

        第一层用完整标题（不分词），第二层才是正文关键词；关键词搜到的多为概念图，
        只在标题层凑不满配额时才补。同时按内容指纹去重，保证 N 张素材是 N 张不同的图。
        """
        if want <= 0:
            return [], ""
        out_dir = directory / IMG_DIRNAME
        saved: List[Dict[str, Any]] = []
        fingerprints: List[str] = []
        stat = {"candidates": 0, "download_failed": 0, "process_failed": 0,
                "too_small": 0, "text_plate": 0, "logo": 0, "duplicates": 0,
                "irrelevant": 0}

        floor = float(getattr(config, "MEDIA_IMG_MATCH_MIN", 0.0) or 0.0)
        hard_floor = floor
        soft_floor = float(getattr(config, "MEDIA_IMG_MATCH_MIN_SOFT", 0.0) or 0.0)
        # 保底数：素材最终要人工审核（有些图不能直接上版），所以不合格也要留够候选
        min_need = min(want, max(1, int(getattr(config, "MEDIA_IMAGE_MIN", 0) or 0)))
        weak = {"n": 0}

        async def absorb(cands) -> None:
            stat["candidates"] += len(cands)
            for item in cands:
                if len(saved) >= want:
                    return
                # 相关度不够的图直接丢：宁可少给，也不给「图不对文」的素材。
                # 原文自带配图天然相关（match=1.0），不受此限。
                if floor and item.provider != "origin" and item.extra.get("match", 1.0) < floor:
                    stat["irrelevant"] += 1
                    continue
                # 百度等引擎一个结果会给多个直链，主链过期时按顺序回退
                urls = [item.url] + list(item.extra.get("alt_urls") or [])
                data = await download.fetch_image_any(client, urls, item.page_url)
                if not data or len(data) < 2048:
                    stat["download_failed"] += 1
                    continue
                w0, h0 = process.image_size(data)
                if 0 < w0 < config.MEDIA_MIN_IMAGE[0] or 0 < h0 < config.MEDIA_MIN_IMAGE[1]:
                    stat["too_small"] += 1          # 图标 / 缩略图，不做素材
                    continue
                if config.IMG_REJECT_TEXT_PLATE and process.is_text_plate(data):
                    stat["text_plate"] += 1         # 整张都是字（通报截图等），不带字样要求
                    continue
                if getattr(config, "IMG_REJECT_LOGO", True) and process.is_logo_banner(data):
                    stat["logo"] += 1               # 台标 / 媒体 logo / 栏目横幅，与正文无关
                    continue
                if config.MEDIA_DEDUP:
                    fp = process.image_fingerprint(data)
                    if process.duplicate_of(fp, fingerprints):
                        stat["duplicates"] += 1     # 内容与已选素材重复（不同 URL 同一张图）
                        continue
                    fingerprints.append(fp)
                target = out_dir / f"{len(saved) + 1:02d}.jpg"
                try:
                    info = await asyncio.to_thread(process.process_image, data, target)
                except Exception:  # noqa: BLE001
                    stat["process_failed"] += 1
                    continue
                rec = {
                    "name": target.name, "url": "/output/" + _rel(target), "path": str(target),
                    "source_url": item.url, "page_url": item.page_url,
                    "platform": item.platform or platforms.platform_of_host(platforms.host_of(item.url)),
                    "provider": item.provider, "keyword": item.keyword,
                    "width": info.get("width", 0), "height": info.get("height", 0),
                    "bytes": info.get("bytes", 0), "match": item.extra.get("match", 0),
                    "source_title": item.title or "",
                    "trimmed_bands": info.get("trimmed_bands") or [],
                }
                # 弱相关标记：低于硬门槛、仅为凑够保底数才留下的候选，审核时可优先剔除
                if (hard_floor and item.provider != "origin"
                        and item.extra.get("match", 1.0) < hard_floor):
                    rec["weak"] = True
                    weak["n"] += 1
                saved.append(rec)
                _insert_asset(article_id, "image", rec, "ready")

        # 只用最靠前的 3 个查询词（完整标题 + 标题主体），越靠后越容易跑偏
        qs = (title_qs or [])[:3] or [title]
        kqs = (keyword_qs or [])[:3]
        # 1) 原文自带配图：就是这篇文章自己的图，相关性最高
        if config.MEDIA_USE_ORIGIN_IMAGES:
            await absorb(await search.origin_images(client, url, title, origin, want))
        # 2) 同源报道配图（按完整标题找到同一事件的其它报道，取该新闻的真实配图）
        if len(saved) < want:
            await absorb(await search.search_news_images(client, title, qs,
                                                         want - len(saved), origin))
        # 3) 标题层：百度 / 必应图片检索
        if len(saved) < want:
            await absorb(await search.search_images(client, qs, want - len(saved),
                                                    origin, mode="targeted", title=title))
        # 4) 标题层：通用图库兜底
        if len(saved) < want:
            await absorb(await search.search_images(client, qs, want - len(saved),
                                                    origin, mode="general", title=title))
        # 5) 关键词层兜底：只用完整的关键词（泛化概念图），相关度门槛更严（见 strict）
        if config.MEDIA_KEYWORD_FALLBACK and kqs and len(saved) < want:
            await absorb(await search.search_images(client, kqs, want - len(saved),
                                                    origin, mode="targeted",
                                                    title=title, strict=True))
        if config.MEDIA_KEYWORD_FALLBACK and kqs and len(saved) < want:
            await absorb(await search.search_images(client, kqs, want - len(saved),
                                                    origin, mode="general",
                                                    title=title, strict=True))
        # 6) 保底补采：凑不够保底数就放宽相关度、逐个查询词单独检索，把候选面撑开。
        #    素材最终要人工审核，缺图比弱相关图更耽误排版，所以这里宁可多给几张
        #    （弱相关图会带 weak 标记，审核时优先剔除即可）。
        if len(saved) < min_need and soft_floor and soft_floor < floor:
            floor = soft_floor
        if len(saved) < min_need:
            single_qs = list(dict.fromkeys([*(title_qs or [])[:3], *(keyword_qs or [])[:3]]))
            for q in single_qs:
                if len(saved) >= min_need:
                    break
                remain = max(want - len(saved), 4)
                await absorb(await search.search_images(client, [q], remain, origin,
                                                        mode="targeted", title=title))
                if len(saved) >= min_need:
                    break
                await absorb(await search.search_images(client, [q], remain, origin,
                                                        mode="general", title=title))
        # 7) 仍不足：用标题实词拼一个短查询再搜一轮（长标题在搜索引擎里召回很差）
        if len(saved) < min_need and title:
            combo = " ".join(keywords.title_entities(title, limit=3))
            if combo:
                remain = max(want - len(saved), 4)
                await absorb(await search.search_images(client, [combo], remain, origin,
                                                        mode="targeted", title=title))
                if len(saved) < min_need:
                    await absorb(await search.search_images(client, [combo], remain, origin,
                                                            mode="general", title=title))
        note = ""
        if len(saved) < min_need:
            # 保底都没凑够才报「不足」，否则只是没到目标数（正常现象，不打扰用户）
            note = (f"图片只取到 {len(saved)} 张，低于保底 {min_need} 张："
                    f"目标 {want}，候选 {stat['candidates']}，相关度不足 {stat['irrelevant']}，"
                    f"下载失败 {stat['download_failed']}，过小 {stat['too_small']}，"
                    f"纯文字图 {stat['text_plate']}，台标/横幅 {stat['logo']}，"
                    f"重复 {stat['duplicates']}，加工失败 {stat['process_failed']}；"
                    f"建议换关键词重采或手动补图")
        elif weak["n"]:
            note = (f"图片 {len(saved)}/{want} 张，其中 {weak['n']} 张为弱相关兜底"
                    f"（只为凑够保底数），排版时建议优先剔除")
        return saved, note

    async def _collect_videos(self, client, directory: Path, title_qs: List[str],
                              origin: str, want: int, article_id: int, title: str = ""):
        """视频只用「完整标题」层检索 —— 关键词搜出来的视频基本与事件无关。

        按完整标题搜不到可用视频时不退到关键词层：宁缺勿滥，不用错视频凑数。
        """
        if want <= 0:
            return [], ""
        cands = []
        # 1) 同源报道页里嵌的新闻视频：媒体自己发的片子，天然与标题一致
        if getattr(config, "MEDIA_USE_NEWS_VIDEOS", True):
            try:
                cands = await search.search_news_videos(client, title, title_qs, want, origin)
            except Exception:  # noqa: BLE001
                cands = []
        # 2) 完整标题层检索（B 站 / Pexels），相关度不足的已被 search_videos 过滤掉
        if len(cands) < want:
            cands += await search.search_videos(client, title_qs, want, origin, title=title)
        out_dir = directory / VID_DIRNAME
        saved: List[Dict[str, Any]] = []
        note = ""
        idx = 0
        for item in cands:
            if len(saved) >= want:
                break
            src, headers = "", {}
            if item.url:
                src, headers = item.url, {}
            elif item.provider == "bilibili" and item.extra.get("bvid"):
                src, headers = await download.resolve_bilibili(client, item.extra["bvid"])
                if not src:
                    note = "B 站视频需登录才能下载，已改用封面图补充"
                    data = await download.fetch_image(client, item.extra.get("cover") or "",
                                                      item.page_url)
                    if data:
                        idx += 1
                        # 单独用 cover- 前缀：视频封面不能和图片素材同名，
                        # 否则会覆盖已经采好的 01.jpg 等（素材看着变少 / 变成重复封面）
                        target = (directory / IMG_DIRNAME) / f"cover-{idx:02d}.jpg"
                        while target.exists():
                            idx += 1
                            target = (directory / IMG_DIRNAME) / f"cover-{idx:02d}.jpg"
                        try:
                            await asyncio.to_thread(process.process_image, data, target)
                            rec = {
                                "name": target.name, "url": "/output/" + _rel(target),
                                "path": str(target), "source_url": item.extra.get("cover") or "",
                                "page_url": item.page_url, "platform": "哔哩哔哩",
                                "provider": "bilibili", "keyword": item.keyword,
                                "width": 0, "height": 0, "bytes": target.stat().st_size,
                                "match": item.extra.get("match", 0),
                            }
                            saved.append(rec)
                            _insert_asset(article_id, "image", rec, "ready")
                        except Exception:  # noqa: BLE001
                            idx -= 1
                    continue
            if not src:
                continue
            idx += 1
            target = out_dir / f"{idx:02d}.mp4"
            try:
                info = await asyncio.to_thread(process.process_video, src, target, headers, 0, True)
            except Exception as exc:  # noqa: BLE001
                note = f"视频处理失败：{str(exc)[:120]}"
                idx -= 1
                continue
            frames = await asyncio.to_thread(
                process.extract_frames, target, directory / FRAME_DIRNAME / f"{idx:02d}")
            rel = _rel(target)
            rec = {
                "name": target.name, "url": "/output/" + rel, "path": str(target),
                "source_url": src, "page_url": item.page_url,
                "title": item.title, "platform": item.platform, "provider": item.provider,
                "keyword": item.keyword, "match": item.extra.get("match", 0),
                "width": info.get("width", 0), "height": info.get("height", 0),
                "bytes": info.get("bytes", 0), "duration": round(info.get("duration", 0), 2),
                "start": round(info.get("start", 0), 2),
                "frames": [{"url": "/output/" + _rel(Path(f["path"])), "name": Path(f["path"]).name}
                           for f in frames],
            }
            saved.append(rec)
            _insert_asset(article_id, "video", rec, "ready")
            for f in frames:
                _insert_asset(article_id, "frame", {
                    "name": Path(f["path"]).name, "url": "/output/" + _rel(Path(f["path"])),
                    "path": f["path"], "title": item.title,
                    "page_url": item.page_url, "platform": item.platform,
                    "provider": item.provider, "keyword": item.keyword,
                    "bytes": f.get("bytes", 0), "match": item.extra.get("match", 0),
                    "width": f.get("width", 0), "height": f.get("height", 0),
                    "trimmed_bands": f.get("trimmed_bands") or [],
                }, "ready")
        if not saved and want > 0:
            # 视频只按完整标题检索，标题冷门或没有对应片子时搜不到属正常情况，
            # 只作提示记录，不能因此把整篇文章判成素材异常。
            note = (note + "；" if note else "")
            note += ("无视频素材（标题未匹配到相关视频，属正常情况，"
                     "不发视频或改用图片排版即可）")
        return saved, note

    # ---------------- 状态 ----------------
    @staticmethod
    def _mark(article_id: int, status: str, error: str, count: Optional[int] = None) -> None:
        with write_conn() as conn:
            if count is None:
                conn.execute(
                    "UPDATE articles SET media_status=?, media_error=?, updated_at=? WHERE id=?",
                    (status, (error or "")[:400], _now(), article_id),
                )
            else:
                conn.execute(
                    """UPDATE articles SET media_status=?, media_error=?, media_count=?,
                       media_at=?, updated_at=? WHERE id=?""",
                    (status, (error or "")[:400], count, _now(), _now(), article_id),
                )


# ---------------------------------------------------------------- 辅助
def _load_article(article_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    return dict(row) if row else None


def _has_rewrite(article_id: int) -> bool:
    """是否已有合格改写稿（没有的话交付一定失败，不该触发）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM article_rewrites WHERE article_id=? AND status='success' LIMIT 1",
            (article_id,)).fetchone()
    return row is not None


def _article_text(article: Dict[str, Any]) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT path FROM raw_cache WHERE id=?",
                           (article.get("content_id") or 0,)).fetchone()
    if not row:
        return ""
    p = Path(row["path"] or "")
    if not p.exists():
        return ""
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("text") or ""
    except Exception:  # noqa: BLE001
        return ""


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(config.OUTPUT_DIR.resolve()).as_posix()
    except ValueError:
        return path.name


def _insert_asset(article_id: int, kind: str, rec: Dict[str, Any], status: str) -> None:
    with write_conn() as conn:
        conn.execute(
            """INSERT INTO media_assets
               (article_id, kind, keyword, provider, platform, src_url, page_url, path, url,
                width, height, bytes, duration, match, clip_start, bands, title, name,
                status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                article_id, kind, rec.get("keyword", ""), rec.get("provider", ""),
                rec.get("platform", ""), rec.get("source_url", ""), rec.get("page_url", ""),
                rec.get("path", ""), rec.get("url", ""), int(rec.get("width") or 0),
                int(rec.get("height") or 0), int(rec.get("bytes") or 0),
                float(rec.get("duration") or 0), float(rec.get("match") or 0),
                float(rec.get("start") or 0),
                ",".join(rec.get("trimmed_bands") or []),
                str(rec.get("title") or rec.get("source_title") or ""),
                str(rec.get("name") or Path(rec.get("path") or "").name or ""),
                status, _now(),
            ),
        )


def clear_assets(article_id: int) -> int:
    """删除一篇文章的素材文件与记录。"""
    with write_conn() as conn:
        rows = conn.execute(
            "SELECT id, path FROM media_assets WHERE article_id=?", (article_id,)
        ).fetchall()
        for r in rows:
            p = Path(r["path"] or "")
            try:
                if p.exists() and config.OUTPUT_DIR.resolve() in p.resolve().parents:
                    p.unlink()
            except OSError:
                pass
        conn.execute("DELETE FROM media_assets WHERE article_id=?", (article_id,))
        conn.execute(
            "UPDATE articles SET media_status='pending', media_error='', media_count=0 WHERE id=?",
            (article_id,),
        )
    return len(rows)


def delete_asset(article_id: int, asset_id: int) -> bool:
    """删除单条素材（文件 + 记录）。

    自动过滤总会漏掉个别「看着就不对」的图（栏目片头 / 台标截图），
    留一个手动剔除入口比把阈值调到误杀正常图更划算。
    """
    with write_conn() as conn:
        row = conn.execute(
            "SELECT path FROM media_assets WHERE id=? AND article_id=?",
            (asset_id, article_id),
        ).fetchone()
        if not row:
            return False
        p = Path(row["path"] or "")
        try:
            if p.exists() and config.OUTPUT_DIR.resolve() in p.resolve().parents:
                p.unlink()
        except OSError:
            pass
        conn.execute("DELETE FROM media_assets WHERE id=?", (asset_id,))
    # 删完最后一张时状态回落成「无素材」，行内按钮与重采入口才不会自相矛盾
    refresh_media_state(article_id)
    return True


def add_local_asset(article_id: int, kind: str, path: Path, name: str = "",
                    width: int = 0, height: int = 0) -> Dict[str, Any]:
    """登记一个手工上传的素材（文件已由调用方落盘）。

    自动检索不可能次次都对题，留一个「自己传」的兜底入口：
    上传的素材直接算 ready、相关度给满分，并并入图片/视频池供排版取用。
    """
    rel = _rel(path)
    rec = {
        "name": name or path.name,
        "url": "/output/" + rel,
        "path": str(path),
        "source_url": "", "page_url": "",
        "platform": "本地上传", "provider": "upload",
        "keyword": "手动上传",
        "width": int(width or 0), "height": int(height or 0),
        "bytes": path.stat().st_size if path.exists() else 0,
        "match": 1.0,
    }
    _insert_asset(article_id, kind, rec, "ready")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM media_assets WHERE article_id=? ORDER BY id DESC LIMIT 1",
            (article_id,)).fetchone()
    total = refresh_media_state(article_id)
    return {"id": int(row["id"]) if row else 0, "total": total, "url": rec["url"]}


def refresh_media_state(article_id: int) -> int:
    """按 media_assets 实有数量回写 articles 的素材状态（上传 / 删除单条后调用）。"""
    with write_conn() as conn:
        n = int(conn.execute(
            "SELECT COUNT(*) c FROM media_assets WHERE article_id=?", (article_id,)
        ).fetchone()["c"])
        conn.execute(
            """UPDATE articles SET media_status=?, media_error='', media_count=?,
               media_at=?, updated_at=? WHERE id=?""",
            ("success" if n else "empty", n, _now(), _now(), article_id),
        )
    return n


def list_assets(article_id: int) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM media_assets WHERE article_id=? ORDER BY kind, id", (article_id,)
        ).fetchall()
    items = [dict(r) for r in rows]
    for d in items:
        d["start"] = round(float(d.pop("clip_start", 0) or 0), 2)
        d["trimmed_bands"] = [x for x in (d.pop("bands", "") or "").split(",") if x]
    return items


engine = MediaEngine()
