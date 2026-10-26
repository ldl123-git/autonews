"""HTTP 接口：上传解析、资讯列表、抓取调度、缓存区。"""
import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import (accounts, cleanup, exporter, finish, importer, insight, layout, llm, rewriter,
               sensitive, style)
from . import publish
from . import settings as app_settings
from .publish import clipboard as publish_clipboard
from .publish import jobs as publish_jobs
from .config import CACHE_DIR, MEDIA_ENABLED, OUTPUT_DIR, UPLOAD_DIR
from .db import get_conn, write_conn
from .fetcher import engine
from .media.collector import (IMG_DIRNAME, VID_DIRNAME, add_local_asset, clear_assets,
                              delete_asset, engine as media_engine, list_assets)
from .rewriter import engine as rewrite_engine

router = APIRouter()

STATUS_GROUPS = {
    "pending": ("pending", "queued", "fetching"),
    "fetching": ("queued", "fetching"),
    "success": ("success",),
    "failed": ("failed",),
}
ALLOWED_STATUS = ("pending", "queued", "fetching", "success", "failed")


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 筛选条件
def _build_where(q: str, status: str, category: str, source: str, date: str = "",
                 content_type: str = "", date_empty: int = 0) -> (str, List[Any]):
    clauses: List[str] = []
    args: List[Any] = []
    if q:
        like = f"%{q}%"
        clauses.append("(title LIKE ? OR source LIKE ? OR author LIKE ? OR url LIKE ?)")
        args += [like, like, like, like]
    if status and status in ALLOWED_STATUS:
        group = STATUS_GROUPS.get(status, (status,))
        clauses.append(f"fetch_status IN ({','.join('?' * len(group))})")
        args += list(group)
    if category:
        clauses.append("(category = ? OR orig_category = ?)")
        args += [category, category]
    if source:
        clauses.append("source = ?")
        args.append(source)
    if date:
        clauses.append("publish_date = ?")
        args.append(date)
    elif date_empty:
        # 「未标注日期」分组：表格里没填发布时间，翻页时不能退回查全表
        clauses.append("(publish_date = '' OR publish_date IS NULL)")
    if content_type:
        clauses.append("content_type = ?")
        args.append(content_type)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


SORT_MAP = {
    "read": "read_count DESC, publish_time DESC",
    "like": "like_count DESC, read_count DESC",
    "time": "publish_time DESC, read_count DESC",
    # 默认：按阅读量从高到低（同阅读量时按时间）
    "default": "read_count DESC, publish_time DESC",
}


def _order_by(sort: str) -> str:
    return SORT_MAP.get(sort, SORT_MAP["default"])


def _rows_to_dicts(rows) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 上传与导入
@router.post("/upload")
async def upload(files: List[UploadFile] = File(...)) -> JSONResponse:
    if not files:
        raise HTTPException(400, "未选择文件")
    results = []
    for file in files:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in (".xlsx", ".xlsm", ".xls", ".csv"):
            results.append({"filename": file.filename, "error": "仅支持 xlsx / xls / csv"})
            continue
        safe = (file.filename or "upload").replace("/", "_").replace("\\", "_")
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S%f")
        target = UPLOAD_DIR / f"{stamp}-{safe}"
        size = 0
        async with aiofiles.open(target, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                await out.write(chunk)
        await file.close()
        batch_id = importer.create_batch(file.filename or safe, size)
        results.append({"batch_id": batch_id, "filename": file.filename, "size": size})
        import threading
        threading.Thread(target=importer.run_import, args=(batch_id, target), daemon=True).start()
    return JSONResponse({"batches": results})


@router.get("/batches")
def list_batches() -> Dict[str, Any]:
    return {"items": importer.list_batches()}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int) -> Dict[str, Any]:
    batch = importer.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    return batch


@router.delete("/batches/{batch_id}")
def delete_batch(batch_id: int) -> Dict[str, Any]:
    with write_conn() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM articles WHERE batch_id=?", (batch_id,))]
        for aid in ids:
            row = conn.execute("SELECT path FROM raw_cache WHERE article_id=?", (aid,)).fetchone()
            if row:
                _delete_cache_file(row["path"])
        if ids:
            q = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM raw_cache WHERE article_id IN ({q})", ids)
            conn.execute(f"DELETE FROM articles WHERE id IN ({q})", ids)
        conn.execute("DELETE FROM batches WHERE id=?", (batch_id,))
    return {"deleted": batch_id}


# ---------------------------------------------------------------- 资讯列表
@router.get("/articles")
def list_articles(date: str = "", status: str = "", q: str = "", category: str = "",
                  source: str = "", content_type: str = "", sort: str = "default",
                  page: int = 1, page_size: int = 50, date_empty: int = 0) -> Dict[str, Any]:
    where, args = _build_where(q, status, category, source, date, content_type, date_empty)
    offset = max(page - 1, 0) * max(1, page_size)
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM articles{where}", args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM articles{where} ORDER BY {_order_by(sort)} LIMIT ? OFFSET ?",
            args + [page_size, offset],
        ).fetchall()
    return {"items": _rows_to_dicts(rows), "total": total, "page": page, "page_size": page_size}


@router.get("/articles/dates")
def list_dates(q: str = "", status: str = "", category: str = "", source: str = "",
               content_type: str = "") -> Dict[str, Any]:
    where, args = _build_where(q, status, category, source, "", content_type)
    sql = f"""
        SELECT publish_date AS date,
               COUNT(*) AS total,
               SUM(CASE WHEN fetch_status='success' THEN 1 ELSE 0 END) AS success,
               SUM(CASE WHEN fetch_status='failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN fetch_status IN ('pending','queued','fetching') THEN 1 ELSE 0 END) AS pending,
               SUM(read_count) AS reads
        FROM articles{where}
        GROUP BY publish_date
        ORDER BY publish_date DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.get("/articles/grouped")
def list_grouped(q: str = "", status: str = "", category: str = "", source: str = "",
                 content_type: str = "", sort: str = "default", limit: int = 20) -> Dict[str, Any]:
    """按日期归类，每个日期返回前 limit 条。"""
    dates = list_dates(q, status, category, source, content_type)["items"]
    where, args = _build_where(q, status, category, source, "", content_type)
    order = _order_by(sort)
    groups = []
    with get_conn() as conn:
        for d in dates:
            date = d["date"]
            clause = (where + " AND " if where else " WHERE ") + "publish_date = ?"
            rows = conn.execute(
                f"SELECT * FROM articles{clause} ORDER BY {order} LIMIT ?", args + [date, limit]
            ).fetchall()
            groups.append({
                "date": date,
                "stats": d,
                "items": _rows_to_dicts(rows),
                "shown": len(rows),
                "total": d["total"],
                "has_more": d["total"] > len(rows),
            })
    return {"groups": groups, "total": sum(g["total"] for g in groups)}


@router.patch("/articles/{article_id}")
def update_article(article_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {"category", "title", "source", "orig_category", "content_type", "url"}
    updates = {k: v for k, v in (payload or {}).items() if k in allowed and isinstance(v, (str, int))}
    if not updates:
        raise HTTPException(400, "没有可更新的字段")
    sets = ", ".join(f"{k}=?" for k in updates)
    with write_conn() as conn:
        conn.execute(
            f"UPDATE articles SET {sets}, updated_at=? WHERE id=?",
            list(updates.values()) + [_now(), article_id],
        )
        row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not row:
        raise HTTPException(404, "记录不存在")
    return dict(row)


@router.delete("/articles/{article_id}")
def delete_article(article_id: int) -> Dict[str, Any]:
    """删除一篇文章及其全部产物：缓存原文 / 素材文件 / 素材记录 / 导出目录。"""
    from .media.collector import clear_assets
    # 1) 素材文件 + media_assets 记录
    cleared = clear_assets(article_id)
    # 2) 缓存原文文件 + raw_cache 记录
    with write_conn() as conn:
        row = conn.execute("SELECT path FROM raw_cache WHERE article_id=?", (article_id,)).fetchone()
        if row:
            _delete_cache_file(row["path"])
        conn.execute("DELETE FROM raw_cache WHERE article_id=?", (article_id,))
        out_dir = conn.execute("SELECT output_dir FROM articles WHERE id=?", (article_id,)).fetchone()
        conn.execute("DELETE FROM articles WHERE id=?", (article_id,))
    # 3) 导出目录（output/<标题> 整个文件夹）
    if out_dir and out_dir["output_dir"]:
        import shutil
        d = OUTPUT_DIR / out_dir["output_dir"]
        if d.exists() and d.resolve() != OUTPUT_DIR.resolve():
            shutil.rmtree(d, ignore_errors=True)
    return {"deleted": article_id, "cleared_assets": cleared}


class DeleteArticlesRequest(BaseModel):
    ids: Optional[List[int]] = None
    date: Optional[str] = None
    status: Optional[str] = None
    q: Optional[str] = None
    category: Optional[str] = None


@router.delete("/articles")
def delete_articles(payload: DeleteArticlesRequest) -> Dict[str, Any]:
    """批量删除文章：按 ids，或按 date/q/category/status 过滤。"""
    ids: List[int] = list(payload.ids or [])
    if not ids:
        where, args = _build_where(payload.q or "", payload.status or "",
                                   payload.category or "", "", payload.date or "")
        with get_conn() as conn:
            ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM articles{where}", args).fetchall()]
    if not ids:
        return {"deleted": 0}
    from .media.collector import clear_assets
    cleared = 0
    for aid in ids:
        cleared += clear_assets(aid)
    qmarks = ",".join("?" * len(ids))
    with write_conn() as conn:
        rows = conn.execute(
            f"SELECT path FROM raw_cache WHERE article_id IN ({qmarks})", ids).fetchall()
        for r in rows:
            _delete_cache_file(r["path"])
        conn.execute(f"DELETE FROM raw_cache WHERE article_id IN ({qmarks})", ids)
        out_dirs = conn.execute(
            f"SELECT DISTINCT output_dir FROM articles WHERE id IN ({qmarks}) AND output_dir<>''",
            ids).fetchall()
        conn.execute(f"DELETE FROM articles WHERE id IN ({qmarks})", ids)
    import shutil
    for r in out_dirs:
        d = OUTPUT_DIR / r["output_dir"]
        if d.exists() and d.resolve() != OUTPUT_DIR.resolve():
            shutil.rmtree(d, ignore_errors=True)
    return {"deleted": len(ids), "cleared_assets": cleared}


# ---------------------------------------------------------------- 抓取
class FetchRequest(BaseModel):
    ids: Optional[List[int]] = None
    date: Optional[str] = None
    status: Optional[str] = None
    q: Optional[str] = None
    category: Optional[str] = None
    all: bool = False


@router.post("/fetch")
def fetch_articles(payload: FetchRequest) -> Dict[str, Any]:
    ids: List[int] = list(payload.ids or [])
    if not ids:
        where, args = _build_where(payload.q or "", payload.status or "", payload.category or "",
                                   "", payload.date or "")
        if not payload.status:
            # 未指定状态时：只抓「未抓取」和「抓取失败」的，避免重复排队
            where = (where + " AND " if where else " WHERE ") + "fetch_status IN ('pending','failed')"
        sql = f"SELECT id FROM articles{where} ORDER BY publish_time DESC"
        with get_conn() as conn:
            ids = [r["id"] for r in conn.execute(sql, args).fetchall()]
    return {"enqueued": engine.enqueue(ids), "queued": engine.stats()["queued"]}


@router.post("/fetch/status")
def fetch_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    ids = [int(i) for i in (payload or {}).get("ids", []) if str(i).isdigit()]
    if not ids:
        return {"items": {}}
    q = ",".join("?" * len(ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, fetch_status, fetch_error, content_type, content_id FROM articles WHERE id IN ({q})", ids
        ).fetchall()
    return {"items": {r["id"]: dict(r) for r in rows}, "queue": engine.stats()}


@router.get("/pipeline")
def pipeline(limit: int = 8, days: int = 14) -> Dict[str, Any]:
    """生产流水线看板：抓取 / 素材 / 改写 / 已交付 / 异常 五个阶段的队列与最近条目。

    阶段是「动态」的：导入后立刻出现在待抓，抓完自动进采样，采完进改写，
    改完进已交付；失败的任何一步落到「异常」列，不会从看板上消失。
    素材为空（media_status='empty'）表示按标题关键词没检索到图/视频，
    属于正常结果，直接进改写，不进异常列。
    """
    limit = max(1, min(50, int(limit or 8)))
    days = max(1, min(90, int(days or 14)))
    cutoff = (dt.datetime.now() - dt.timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00:00")
    cols = ("id,title,source,source_norm,fetch_status,fetch_error,media_status,media_error,"
            "rewrite_status,rewrite_error,finish_status,finish_dir,finish_at,sensitive_status")
    # 抓取中 / 排队中的排前面，其余按 id 倒序
    fetch_order = ("CASE fetch_status WHEN 'fetching' THEN 0 WHEN 'queued' THEN 1"
                   " ELSE 2 END, id DESC")
    failed_where = ("fetch_status='failed' OR media_status='failed'"
                    " OR rewrite_status='failed' OR finish_status='failed'")
    media_ready = "COALESCE(media_status,'') IN ('success','empty')"
    with get_conn() as conn:
        def rows(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

        def count(sql: str, args: tuple = ()) -> int:
            return int(conn.execute(sql, args).fetchone()["c"])

        stages = {
            "fetch": rows(
                f"SELECT {cols} FROM articles"
                " WHERE fetch_status IN ('pending','queued','fetching')"
                f" ORDER BY {fetch_order} LIMIT ?", (limit,)),
            "media": rows(
                f"SELECT {cols} FROM articles WHERE fetch_status='success'"
                " AND COALESCE(media_status,'') IN ('','queued','running')"
                " ORDER BY id DESC LIMIT ?", (limit,)),
            "rewrite": rows(
                f"SELECT {cols} FROM articles WHERE {media_ready}"
                " AND COALESCE(rewrite_status,'') IN ('','queued','running')"
                " ORDER BY id DESC LIMIT ?", (limit,)),
            "done": rows(
                f"SELECT {cols} FROM articles WHERE finish_status='success'"
                " ORDER BY finish_at DESC LIMIT ?", (limit,)),
            "failed": rows(
                f"SELECT {cols} FROM articles WHERE {failed_where}"
                " ORDER BY id DESC LIMIT ?", (limit,)),
        }
        counts = {
            "fetch": count("SELECT COUNT(*) c FROM articles"
                           " WHERE fetch_status IN ('pending','queued','fetching')"),
            "fetch_pending": count("SELECT COUNT(*) c FROM articles WHERE fetch_status='pending'"),
            "fetch_running": count("SELECT COUNT(*) c FROM articles"
                                   " WHERE fetch_status IN ('queued','fetching')"),
            "media": count("SELECT COUNT(*) c FROM articles WHERE fetch_status='success'"
                           " AND COALESCE(media_status,'') IN ('','queued','running')"),
            "media_running": count("SELECT COUNT(*) c FROM articles"
                                   " WHERE COALESCE(media_status,'') IN ('queued','running')"),
            "media_empty": count("SELECT COUNT(*) c FROM articles WHERE media_status='empty'"),
            "rewrite": count(f"SELECT COUNT(*) c FROM articles WHERE {media_ready}"
                             " AND COALESCE(rewrite_status,'') IN ('','queued','running')"),
            "rewrite_running": count("SELECT COUNT(*) c FROM articles"
                                     " WHERE COALESCE(rewrite_status,'') IN ('queued','running')"),
            "done": count("SELECT COUNT(*) c FROM articles WHERE finish_status='success'"),
            "failed": count(f"SELECT COUNT(*) c FROM articles WHERE {failed_where}"),
            "total": count("SELECT COUNT(*) c FROM articles"),
        }
        # 分类统计：按来源（头条 / 百家 / 其他）和按分类看产出，
        # 与发布中心的「按账号 / 按入口」图表保持一致的口径。
        by_source = rows(
            f"""SELECT COALESCE(NULLIF(source_norm,''), 'other') AS name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN finish_status='success' THEN 1 ELSE 0 END) AS done,
                       SUM(CASE WHEN {failed_where} THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN media_status='empty' THEN 1 ELSE 0 END) AS media_empty
                FROM articles GROUP BY 1 ORDER BY total DESC""")
        by_category = rows(
            f"""SELECT COALESCE(NULLIF(category,''), NULLIF(orig_category,''), '未分类') AS name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN finish_status='success' THEN 1 ELSE 0 END) AS done,
                       SUM(CASE WHEN {failed_where} THEN 1 ELSE 0 END) AS failed
                FROM articles GROUP BY 1 ORDER BY total DESC LIMIT 12""")
        imports = {r["date"]: r for r in rows(
            "SELECT substr(created_at,1,10) AS date, COUNT(*) AS total FROM articles"
            " WHERE created_at >= ? GROUP BY 1 ORDER BY 1", (cutoff,))}
        delivers = {r["date"]: r for r in rows(
            "SELECT substr(finish_at,1,10) AS date, COUNT(*) AS done FROM articles"
            " WHERE finish_status='success' AND finish_at >= ? GROUP BY 1 ORDER BY 1", (cutoff,))}
        daily = [{"date": d,
                  "total": int((imports.get(d) or {}).get("total") or 0),
                  "done": int((delivers.get(d) or {}).get("done") or 0)}
                 for d in sorted(set(imports) | set(delivers))]
    return {
        "counts": counts,
        "queues": {"fetch": engine.stats(), "media": media_engine.stats(),
                   "rewrite": rewrite_engine.stats()},
        "stats": {
            "by_source": by_source,
            "by_category": by_category,
            "daily": daily,
            "days": days,
        },
        "stages": stages,
        "server_time": _now(),
    }


@router.post("/pipeline/run")
def pipeline_run() -> Dict[str, Any]:
    """一键推进：把各阶段可执行的文章都塞进对应队列，worker 自己消费。"""
    with get_conn() as conn:
        fetch_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM articles WHERE fetch_status IN ('pending','failed')").fetchall()]
        media_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM articles WHERE fetch_status='success'"
            " AND media_status IN ('','failed','pending')").fetchall()]
        rewrite_ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM articles WHERE COALESCE(media_status,'') IN ('success','empty')"
            " AND rewrite_status IN ('','failed','pending')").fetchall()]
    return {"enqueued": {
        "fetch": engine.enqueue(fetch_ids) if fetch_ids else 0,
        "media": media_engine.enqueue(media_ids) if media_ids else 0,
        "rewrite": rewrite_engine.enqueue(rewrite_ids) if rewrite_ids else 0,
    }}


@router.get("/fetch/queue")
def fetch_queue() -> Dict[str, Any]:
    return engine.stats()


@router.delete("/fetch/queue")
def clear_queue() -> Dict[str, Any]:
    """清空排队中的任务（正在抓取的会继续完成）。"""
    while True:
        try:
            engine.queue.get_nowait()
            engine.queue.task_done()
        except Exception:  # noqa: BLE001
            break
    with write_conn() as conn:
        conn.execute("UPDATE articles SET fetch_status='pending' WHERE fetch_status='queued'")
    return engine.stats()


# ---------------------------------------------------------------- 缓存区
@router.get("/cache")
def list_cache(q: str = "", content_type: str = "", page: int = 1, page_size: int = 30) -> Dict[str, Any]:
    clauses, args = [], []
    if q:
        like = f"%{q}%"
        clauses.append("(c.title LIKE ? OR c.url LIKE ? OR c.summary LIKE ?)")
        args += [like, like, like]
    if content_type:
        clauses.append("c.content_type = ?")
        args.append(content_type)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    offset = max(page - 1, 0) * page_size
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM raw_cache c{where}", args).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT c.*, a.source, a.category, a.publish_time, a.read_count, a.like_count,
                       a.rewrite_id AS rid, a.rewrite_status AS rst
                FROM raw_cache c LEFT JOIN articles a ON a.id = c.article_id
                {where} ORDER BY c.id DESC LIMIT ? OFFSET ?""",
            args + [page_size, offset],
        ).fetchall()
    return {"items": _rows_to_dicts(rows), "total": total, "page": page, "page_size": page_size}


@router.get("/cache/{cache_id}")
def get_cache(cache_id: int) -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT c.*, a.source, a.category, a.publish_time, a.title AS article_title
               FROM raw_cache c LEFT JOIN articles a ON a.id = c.article_id WHERE c.id=?""",
            (cache_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "缓存不存在")
    data = dict(row)
    path = Path(data.get("path") or "")
    detail: Dict[str, Any] = {}
    if path.exists():
        try:
            detail = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            detail = {}
    data["detail"] = detail
    return data


@router.delete("/cache/{cache_id}")
def delete_cache(cache_id: int) -> Dict[str, Any]:
    with write_conn() as conn:
        row = conn.execute("SELECT * FROM raw_cache WHERE id=?", (cache_id,)).fetchone()
        if row:
            _delete_cache_file(row["path"])
            conn.execute("DELETE FROM raw_cache WHERE id=?", (cache_id,))
            conn.execute("UPDATE articles SET content_id=NULL, fetch_status='pending', fetch_error='' WHERE id=?",
                         (row["article_id"],))
    return {"deleted": cache_id}


@router.delete("/cache")
def clear_cache() -> Dict[str, Any]:
    with write_conn() as conn:
        paths = [r["path"] for r in conn.execute("SELECT path FROM raw_cache").fetchall()]
        for p in paths:
            _delete_cache_file(p)
        conn.execute("DELETE FROM raw_cache")
        conn.execute("UPDATE articles SET content_id=NULL, fetch_status='pending', fetch_error=''")
    return {"cleared": len(paths)}


# ---------------------------------------------------------------- 导出到 output
class ExportRequest(BaseModel):
    ids: Optional[List[int]] = None
    q: Optional[str] = None
    category: Optional[str] = None
    date: Optional[str] = None
    status: Optional[str] = "success"


@router.post("/export")
def export_articles(payload: ExportRequest) -> Dict[str, Any]:
    ids: List[int] = list(payload.ids or [])
    if not ids:
        where, args = _build_where(payload.q or "", payload.status or "success",
                                   payload.category or "", "", payload.date or "")
        with get_conn() as conn:
            ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM articles{where} ORDER BY read_count DESC", args).fetchall()]
    return exporter.export_many(ids)


@router.post("/export/{article_id}")
def export_one(article_id: int) -> Dict[str, Any]:
    return exporter.export_article(article_id)


@router.get("/export/all")
def list_output() -> Dict[str, Any]:
    """已在 output 落盘的目录清单。"""
    items = []
    for meta in sorted(OUTPUT_DIR.glob("*/meta.json")):
        try:
            items.append(json.loads(meta.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------- 素材收集
class MediaRequest(BaseModel):
    ids: Optional[List[int]] = None
    q: Optional[str] = None
    category: Optional[str] = None
    date: Optional[str] = None
    image_count: Optional[int] = None
    video_count: Optional[int] = None
    keywords: Optional[List[str]] = None


@router.post("/media/collect")
def collect_media(payload: MediaRequest) -> Dict[str, Any]:
    if not MEDIA_ENABLED:
        raise HTTPException(400, "素材收集功能已关闭（config.MEDIA_ENABLED=False）")
    ids: List[int] = list(payload.ids or [])
    if not ids:
        # 不限定 fetch_status：未抓取的文章也会入队，由素材 worker 自动补抓原文，
        # 使「抓取素材」与「抓取原文」可以独立、不分先后地批量触发
        where, args = _build_where(payload.q or "", "", payload.category or "",
                                   "", payload.date or "")
        with get_conn() as conn:
            ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM articles{where} ORDER BY read_count DESC", args).fetchall()]
    # 0 表示「用服务端默认配置」（前端各入口默认传 0），统一归一成 None
    opts = {
        "image_count": payload.image_count or None,
        "video_count": payload.video_count or None,
        "keywords": payload.keywords or [],
    }
    return {"enqueued": media_engine.enqueue(ids, opts), "queue": media_engine.stats()}


@router.post("/media/status")
def media_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    ids = [int(i) for i in (payload or {}).get("ids", []) if str(i).isdigit()]
    if not ids:
        return {"items": {}, "queue": media_engine.stats()}
    q = ",".join("?" * len(ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, media_status, media_error, media_count, output_dir FROM articles WHERE id IN ({q})",
            ids,
        ).fetchall()
    return {"items": {r["id"]: dict(r) for r in rows}, "queue": media_engine.stats()}


@router.get("/media/{article_id}")
def get_media(article_id: int) -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT title, output_dir, media_status, media_error, url, source FROM articles WHERE id=?",
            (article_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "记录不存在")
    data = dict(row)
    assets = list_assets(article_id)
    manifest: Dict[str, Any] = {}
    if data.get("output_dir"):
        p = OUTPUT_DIR / data["output_dir"] / "素材/素材清单.json"
        if p.exists():
            try:
                manifest = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                manifest = {}
    keywords_json = manifest.get("keywords") or []
    if isinstance(keywords_json, dict):     # 分层检索：标题层 + 关键词兜底层
        keywords_json = list(keywords_json.get("title") or []) + list(keywords_json.get("fallback") or [])
    opts = manifest.get("options") or {}
    return {
        "article_id": article_id,
        "dir": data.get("output_dir") or "",
        "media_status": data.get("media_status") or "",
        "media_error": data.get("media_error") or "",
        "origin_platform": manifest.get("origin_platform", ""),
        "title_query": manifest.get("title_query", ""),
        "keywords": keywords_json,
        # 图片计划：目标张数 / 保底张数（素材不足时会降门槛补采，见 media.collector）
        "image_target": int(opts.get("image_count") or 0),
        "image_min": int(opts.get("image_min") or 0),
        "items": assets,
        "total": len(assets),
    }


@router.delete("/media/{article_id}")
def delete_media(article_id: int) -> Dict[str, Any]:
    return {"deleted": clear_assets(article_id)}


# 手工上传素材：自动检索总有几次「全都不对题」，得能自己传图 / 传片补进来
_UP_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
_UP_VID_EXTS = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}
_UP_IMG_MAX = 30 * 1024 * 1024
_UP_VID_MAX = 500 * 1024 * 1024


def _unique_upload_path(dir_path: Path, stem: str, suffix: str) -> Path:
    idx = 1
    while True:
        path = dir_path / f"{stem}-{idx:02d}{suffix}"
        if not path.exists():
            return path
        idx += 1


def _image_size(path: Path) -> tuple:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:  # noqa: BLE001 - 读不出尺寸不影响素材可用
        return 0, 0


@router.post("/media/{article_id}/upload")
async def upload_media(article_id: int, background: BackgroundTasks,
                       files: List[UploadFile] = File(...)) -> Dict[str, Any]:
    """上传本地素材（图片 / 视频），直接并入该文章的素材池。"""
    if not files:
        raise HTTPException(400, "未选择文件")
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not row:
        raise HTTPException(404, "记录不存在")
    directory = exporter.article_dir(dict(row))
    img_dir, vid_dir = directory / IMG_DIRNAME, directory / VID_DIRNAME

    added, skipped = 0, []
    for file in files:
        name = file.filename or "upload"
        suffix = Path(name).suffix.lower()
        if suffix in _UP_IMG_EXTS:
            kind, target_dir, limit = "image", img_dir, _UP_IMG_MAX
        elif suffix in _UP_VID_EXTS:
            kind, target_dir, limit = "video", vid_dir, _UP_VID_MAX
        else:
            skipped.append({"filename": name, "reason": "仅支持图片 / 视频文件"})
            await file.close()
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        target = _unique_upload_path(target_dir, "手动", suffix)
        size = 0
        async with aiofiles.open(target, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    break
                await out.write(chunk)
        await file.close()
        if size == 0 or size > limit:
            target.unlink(missing_ok=True)
            skipped.append({"filename": name, "reason": "文件为空或超过大小上限"})
            continue
        width, height = _image_size(target) if kind == "image" else (0, 0)
        add_local_asset(article_id, kind, target, name=name, width=width, height=height)
        added += 1

    if added:
        # 已有合格改写稿时重跑交付，让刚传的素材直接补进成稿（与自动采集一致）
        with get_conn() as conn:
            has_rewrite = conn.execute(
                "SELECT 1 FROM article_rewrites WHERE article_id=? AND status='success' LIMIT 1",
                (article_id,)).fetchone()
        if has_rewrite:
            background.add_task(finish.export_article, article_id)
    return {"ok": added > 0, "added": added, "skipped": skipped,
            "items": list_assets(article_id)}


@router.delete("/media/{article_id}/assets/{asset_id}")
def delete_media_asset(article_id: int, asset_id: int) -> Dict[str, Any]:
    """删除单张素材（文件 + 记录），用于手动剔除个别不对题的图 / 视频。"""
    ok = delete_asset(article_id, asset_id)
    if not ok:
        raise HTTPException(404, "素材不存在")
    return {"ok": True, "items": list_assets(article_id)}


@router.get("/media/queue")
def media_queue() -> Dict[str, Any]:
    return media_engine.stats()


# ---------------------------------------------------------------- 统计与字典
@router.get("/stats")
def stats() -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN fetch_status='success' THEN 1 ELSE 0 END),0) AS success,
                      COALESCE(SUM(CASE WHEN fetch_status='failed' THEN 1 ELSE 0 END),0) AS failed,
                      COALESCE(SUM(CASE WHEN fetch_status IN ('pending','queued','fetching') THEN 1 ELSE 0 END),0) AS pending,
                      COALESCE(SUM(read_count),0) AS reads, COALESCE(SUM(like_count),0) AS likes
               FROM articles"""
        ).fetchone()
        cache = conn.execute(
            """SELECT COUNT(*) AS cached,
                      COALESCE(SUM(CASE WHEN content_type='文章' THEN 1 ELSE 0 END),0) AS articles,
                      COALESCE(SUM(CASE WHEN content_type='图片' THEN 1 ELSE 0 END),0) AS images,
                      COALESCE(SUM(CASE WHEN content_type='视频' THEN 1 ELSE 0 END),0) AS videos
               FROM raw_cache"""
        ).fetchone()
        dates = conn.execute("SELECT COUNT(DISTINCT publish_date) d FROM articles").fetchone()["d"]
        media = conn.execute(
            """SELECT COUNT(*) c, COALESCE(SUM(bytes),0) b,
                      COALESCE(SUM(CASE WHEN kind='image' THEN 1 ELSE 0 END),0) AS images,
                      COALESCE(SUM(CASE WHEN kind='video' THEN 1 ELSE 0 END),0) AS videos
               FROM media_assets"""
        ).fetchone()
        exported = conn.execute(
            "SELECT COUNT(*) c FROM articles WHERE COALESCE(output_dir,'')<>''").fetchone()["c"]
    size = sum(f.stat().st_size for f in CACHE_DIR.glob("*.json") if f.is_file())
    out_size = sum(f.stat().st_size for f in OUTPUT_DIR.rglob("*") if f.is_file())
    return {
        "articles": dict(row),
        "cache": dict(cache),
        "cache_size": size,
        "date_count": dates,
        "queue": engine.stats(),
        "media": {
            "assets": dict(media),
            "exported": exported,
            "output_size": out_size,
            "queue": media_engine.stats(),
        },
    }


@router.get("/categories")
def categories() -> Dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT COALESCE(NULLIF(category,''), orig_category) AS name, COUNT(*) c
               FROM articles GROUP BY name HAVING name <> '' ORDER BY c DESC"""
        ).fetchall()
        sources = conn.execute(
            "SELECT source AS name, COUNT(*) c FROM articles WHERE source<>'' GROUP BY source ORDER BY c DESC"
        ).fetchall()
    return {"categories": [dict(r) for r in rows], "sources": [dict(r) for r in sources]}


# ---------------------------------------------------------------- 文章改写
class RewriteRequest(BaseModel):
    ids: Optional[List[int]] = None
    date: Optional[str] = None
    q: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    only_fetched: bool = True   # 只改已抓取到正文的


@router.post("/rewrite")
def start_rewrite(payload: RewriteRequest) -> Dict[str, Any]:
    ids: List[int] = list(payload.ids or [])
    if not ids:
        where, args = _build_where(payload.q or "", payload.status or "", payload.category or "",
                                   "", payload.date or "")
        clause = where
        if payload.only_fetched:  # 只改已经抓到正文的，避免空跑
            clause = (where + " AND " if where else " WHERE ") + "content_id IS NOT NULL"
        sql = f"SELECT id FROM articles{clause} ORDER BY publish_time DESC"
        with get_conn() as conn:
            ids = [r["id"] for r in conn.execute(sql, args).fetchall()]
    if not ids:
        raise HTTPException(400, "没有可改写的记录（需先抓取成功）")
    if not llm.resolve("article"):
        raise HTTPException(400, "还没有配置文章大模型，请先在「系统设置 → 大模型配置」里添加一个")
    return {"enqueued": rewrite_engine.enqueue(ids), "ids": ids,
            "queued": rewrite_engine.stats()["queued"]}


@router.post("/rewrite/status")
def rewrite_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    ids = [int(i) for i in (payload or {}).get("ids", []) if str(i).isdigit()]
    items: Dict[str, Any] = {}
    if ids:
        q = ",".join("?" * len(ids))
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, rewrite_status, rewrite_error, rewrite_id FROM articles WHERE id IN ({q})", ids
            ).fetchall()
        items = {r["id"]: dict(r) for r in rows}
    return {"items": items, "queue": rewrite_engine.stats()}


@router.get("/rewrite/queue")
def rewrite_queue() -> Dict[str, Any]:
    return rewrite_engine.stats()


@router.delete("/rewrite/queue")
def clear_rewrite_queue() -> Dict[str, Any]:
    while True:
        try:
            rewrite_engine.queue.get_nowait()
            rewrite_engine.queue.task_done()
        except Exception:  # noqa: BLE001
            break
    with write_conn() as conn:
        conn.execute("UPDATE articles SET rewrite_status='' WHERE rewrite_status='queued'")
    return rewrite_engine.stats()


@router.get("/rewrite/{rewrite_id}")
def get_rewrite(rewrite_id: int) -> Dict[str, Any]:
    item = rewriter.detail(rewrite_id)
    if not item:
        raise HTTPException(404, "改写记录不存在")
    return item


@router.get("/rewrite/latest/{article_id}")
def get_latest_rewrite(article_id: int) -> Dict[str, Any]:
    return {"item": rewriter.latest(article_id)}


@router.post("/rewrite/{article_id}/retry")
def retry_rewrite(article_id: int) -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM articles WHERE id=?", (article_id,)).fetchone()
    if not row:
        raise HTTPException(404, "文章不存在")
    return {"enqueued": rewrite_engine.enqueue([article_id])}


@router.delete("/rewrite/{rewrite_id}")
def delete_rewrite(rewrite_id: int) -> Dict[str, Any]:
    if not rewriter.delete(rewrite_id):
        raise HTTPException(404, "改写记录不存在")
    return {"deleted": rewrite_id}


@router.patch("/rewrite/{rewrite_id}/title")
def pick_rewrite_title(rewrite_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """在几个标题候选中挑一个作为最终标题。

    标题命中敏感词时默认拒绝：含敏感词标题的稿子交付闸门也会拦，不如在这里就提示。
    ``force=true`` 可以强行采用，但会标记 blocked，方便页面标红。
    """
    data = payload or {}
    title = str(data.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    if app_settings.get_bool("sensitive.enabled", True):
        report = sensitive.check("", title)
        if not report["ok"]:
            if not data.get("force"):
                raise HTTPException(400, "标题含敏感词：" + sensitive.brief(report)
                                    + "（换个候选，或加 force=true 强制采用）")
            rewriter._update_result(
                rewrite_id, used_title=title, sensitive_status="blocked",
                sensitive_hits=json.dumps(report["hits"], ensure_ascii=False),
                sensitive_checked_at=_now())
            return rewriter.detail(rewrite_id) or {}
    rewriter._update_result(rewrite_id, used_title=title)
    return rewriter.detail(rewrite_id) or {}


# ---------------------------------------------------------------- 文章排版
class LayoutPatch(BaseModel):
    title: Optional[str] = None
    placements: Optional[List[Dict[str, Any]]] = None
    blocks: Optional[List[Dict[str, Any]]] = None    # 人工编辑后的正文块


@router.post("/rewrite/{rewrite_id}/waive")
def waive_rewrite(rewrite_id: int, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """人工放行：把因敏感词判失败的稿子恢复成可用状态，命中词仍然留痕。

    放行后这篇可以正常排版、交付到 output_finish；meta.json 会记下命中词、
    放行时间与备注，事后能追溯是谁批的、放过了哪些词。
    """
    note = str((payload or {}).get("note") or "").strip()[:200]
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM article_rewrites WHERE id=?", (rewrite_id,)).fetchone()
    if not row:
        raise HTTPException(404, "改写记录不存在")
    d = dict(row)
    state = str(d.get("sensitive_status") or "")
    if state == "waived":
        return rewriter.detail(rewrite_id) or {}
    if state != "blocked" and str(d.get("status")) == "success":
        raise HTTPException(400, "这篇没有未通过的敏感词，不需要放行")
    rewriter._update_result(
        rewrite_id, status="success", stage="", error="",
        sensitive_status="waived", sensitive_waived_at=_now(), sensitive_waived_note=note)
    with write_conn() as conn:
        conn.execute(
            "UPDATE articles SET rewrite_status='success', rewrite_error='',"
            " sensitive_status='waived' WHERE id=?", (int(d["article_id"]),))
    return rewriter.detail(rewrite_id) or {}


@router.get("/rewrite/variants/{article_id}")
def rewrite_variants(article_id: int) -> Dict[str, Any]:
    """多平台版本：一篇原文有头条版 / 百家版各一条。"""
    items = rewriter.variants(article_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, source, source_norm FROM articles WHERE id=?", (article_id,)
        ).fetchone()
    return {"items": items,
            "plan": style.explain(row["source"] if row else ""),
            "article": dict(row) if row else None}


# ---------------------------------------------------------------- 敏感词校验
@router.get("/sensitive/stats")
def sensitive_stats() -> Dict[str, Any]:
    """词库统计：启用了哪些档、各文件收了多少词条、构建耗时。"""
    return sensitive.stats()


@router.post("/sensitive/reload")
def sensitive_reload() -> Dict[str, Any]:
    """词库/白名单/设置改完后重建自动机。"""
    return sensitive.reload()


@router.get("/sensitive/check")
def sensitive_check(text: str = "", title: str = "") -> Dict[str, Any]:
    """试跑校验：调词库与白名单时用，不影响任何文章状态。"""
    return sensitive.check(text or "", title or "")


@router.get("/sensitive/checks/{article_id}")
def sensitive_check_log(article_id: int, limit: int = 20) -> Dict[str, Any]:
    """这篇的校验留痕：每轮命中词 + 待修稿 / 修后稿（各截断 600 字）。"""
    limit = max(1, min(100, int(limit or 20)))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, rewrite_id, round, platform, content_form, status, hits, created_at,"
            " substr(before_text,1,600) AS before_text, substr(after_text,1,600) AS after_text"
            " FROM sensitive_checks WHERE article_id=? ORDER BY id DESC LIMIT ?",
            (article_id, limit)).fetchall()
    items: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["hits"] = json.loads(d.get("hits") or "[]")
        except (TypeError, ValueError):
            d["hits"] = []
        items.append(d)
    return {"items": items}


@router.get("/sensitive/hits")
def sensitive_hits(limit: int = 50) -> Dict[str, Any]:
    """历史命中聚合（按词频）：沉淀白名单 / 干扰规则用。

    看哪些词天天命中但明显是正常表述，确认后加进设置页「白名单」，
    比一条条翻单篇校验记录快得多。
    """
    limit = max(1, min(200, int(limit or 50)))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT hits, created_at FROM sensitive_checks ORDER BY id DESC LIMIT 5000"
        ).fetchall()
    agg: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        try:
            hits = json.loads(r["hits"] or "[]")
        except (TypeError, ValueError):
            continue
        for h in hits if isinstance(hits, list) else []:
            word = str(h.get("word") or "")
            if not word:
                continue
            item = agg.get(word)
            if item is None:
                file = h.get("file") or ""
                # 老记录没有 category 字段：按词库文件名补算，页面分组才不会空着
                category = h.get("category") or sensitive._category_of(file)
                agg[word] = {"word": word, "tier": h.get("tier") or "",
                             "file": file, "category": category,
                             "count": 1, "samples": list(h.get("snippets") or [])[:1],
                             "last_at": r["created_at"]}
            else:
                item["count"] += 1
    items = sorted(agg.values(), key=lambda x: (-x["count"], x["word"]))[:limit]
    return {"items": items, "scanned": len(rows)}


@router.post("/sensitive/recheck/{article_id}")
def sensitive_recheck(article_id: int, repair: bool = False,
                      include_waived: bool = False) -> Dict[str, Any]:
    """按当前规则重查这篇的稿子（调完词库 / 白名单 / 干扰规则后复核用）。

    ``repair=true`` 时，因敏感词判失败、但按新规则已经合格的稿子会恢复成 success
    —— 误报治理之后用来批量救回被误杀的稿子，不用整篇重新改写。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, final_text, used_title, status, sensitive_status"
            " FROM article_rewrites WHERE article_id=? AND (status='success'"
            " OR (status='failed' AND sensitive_status='blocked'))"
            " ORDER BY id DESC", (article_id,)).fetchall()
    items: List[Dict[str, Any]] = []
    restored = 0
    for r in rows:
        # 人工放行的稿子默认跳过：重查不该悄悄推翻人的决定（要重估就传 include_waived=true）
        if str(r["sensitive_status"] or "") == "waived" and not include_waived:
            items.append({"rewrite_id": int(r["id"]), "ok": True, "restored": False,
                          "skipped": "人工放行，跳过重查"})
            continue
        report = sensitive.check(str(r["final_text"] or ""), str(r["used_title"] or ""))
        passed = bool(report["ok"])
        fields: Dict[str, Any] = {
            "sensitive_status": "pass" if passed else "blocked",
            "sensitive_hits": json.dumps(report.get("hits") or [], ensure_ascii=False),
            "sensitive_checked_at": _now(),
        }
        back = bool(passed and repair and str(r["status"]) == "failed")
        if back:
            fields.update({"status": "success", "stage": "", "error": ""})
            restored += 1
        rewriter._update_result(int(r["id"]), **fields)
        items.append({"rewrite_id": int(r["id"]), "ok": passed, "restored": back,
                      "blocked": [h["word"] for h in report["blocked"]],
                      "case": [h["word"] for h in report["case"]],
                      "warn": [h["word"] for h in report["warn"]]})
    if items:
        with write_conn() as conn:
            conn.execute("UPDATE articles SET sensitive_status=? WHERE id=?",
                         ("pass" if any(i["ok"] for i in items) else "blocked", article_id))
            if restored:
                conn.execute(
                    "UPDATE articles SET rewrite_status='success', rewrite_error='' WHERE id=?",
                    (article_id,))
    return {"article_id": article_id, "items": items, "restored": restored}


@router.post("/sensitive/recheck_all")
def sensitive_recheck_all(repair: bool = True, scope: str = "failed") -> Dict[str, Any]:
    """批量重查。

    * scope=failed（默认）：只处理「因敏感词失败」的稿子，合格则恢复成 success；
    * scope=all：给所有历史稿补一遍校验状态（改造前的稿子 sensitive_status 是空的）。
    """
    with get_conn() as conn:
        if str(scope) == "all":
            ids = [int(r["article_id"]) for r in conn.execute(
                "SELECT DISTINCT article_id FROM article_rewrites"
                " WHERE article_id > 0").fetchall()]
        else:
            ids = [int(r["article_id"]) for r in conn.execute(
                "SELECT DISTINCT article_id FROM article_rewrites"
                " WHERE status='failed' AND sensitive_status='blocked'").fetchall()]
    restored = 0
    for aid in ids:
        try:
            restored += int(sensitive_recheck(aid, repair=repair).get("restored") or 0)
        except Exception:      # noqa: BLE001 - 单篇失败不影响其它
            continue
    return {"articles": len(ids), "restored": restored}


@router.post("/layout/{article_id}")
def build_layout(article_id: int, rewrite_id: Optional[int] = None,
                 platform: str = "", form: str = "") -> Dict[str, Any]:
    try:
        item = layout.build(article_id, rewrite_id, platform, form)
    except LookupError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"item": item}


@router.get("/layout/{article_id}")
def get_layout(article_id: int, platform: str = "", form: str = "") -> Dict[str, Any]:
    return {"item": layout.latest(article_id, platform, form)}


@router.get("/layout/detail/{layout_id}")
def get_layout_detail(layout_id: int) -> Dict[str, Any]:
    item = layout.detail(layout_id)
    if not item:
        raise HTTPException(404, "排版记录不存在")
    return item


@router.patch("/layout/{layout_id}")
def patch_layout(layout_id: int, payload: LayoutPatch) -> Dict[str, Any]:
    """手动调整：改正文 / 开关插入点 / 换素材 / 挪位置 / 插入素材。

    传 blocks 就是「人工改稿」：会重跑敏感词校验，并把正文写回对应的改写稿
    （交付闸门和自动发布读的是改写稿，两处必须一致）。返回体里的
    sensitive_edit 是本轮校验结果，前端据此标红命中词。
    """
    fields: Dict[str, Any] = {}
    if payload.title is not None:
        fields["title"] = payload.title
    if payload.blocks is not None:
        fields["blocks"] = payload.blocks
    if payload.placements is not None:
        for p in payload.placements:
            p["enabled"] = 1 if p.get("enabled") else 0
            p["after_block"] = int(p.get("after_block") or 0)
        fields["placements"] = payload.placements
    try:
        item = layout.update(layout_id, **fields)
    except LookupError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not item:
        raise HTTPException(404, "排版记录不存在")
    return item


@router.post("/layout/{layout_id}/export")
def export_layout(layout_id: int) -> Dict[str, Any]:
    try:
        return layout.export(layout_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/layout/{layout_id}/assets/refresh")
def refresh_layout_assets(layout_id: int) -> Dict[str, Any]:
    """素材重采后再点排版：把可用素材快照换成最新一批，保留已插入的位置。"""
    item = layout.refresh_assets(layout_id)
    if not item:
        raise HTTPException(404, "排版记录不存在")
    return {"item": item}


@router.delete("/layout/{layout_id}")
def delete_layout(layout_id: int) -> Dict[str, Any]:
    if not layout.delete(layout_id):
        raise HTTPException(404, "排版记录不存在")
    return {"deleted": layout_id}


class CopyBody(BaseModel):
    """一键复制的参数：图片方式 + 图床账号（都可留空，用系统设置里的默认值）。"""
    mode: Optional[str] = None          # inline / upload / text
    platform: Optional[str] = None
    account_id: Optional[int] = None
    max_width: Optional[int] = None
    quality: Optional[int] = None


@router.post("/layout/{layout_id}/copy")
def copy_layout(layout_id: int, payload: Optional[CopyBody] = None) -> Dict[str, Any]:
    """把排版稿做成可直接粘进平台编辑器的富文本（手动发布用）。

    upload 模式会真的去平台传图，所以用 POST；同步端点（FastAPI 走线程池），
    前端按钮期间置灰即可，不占事件循环。
    """
    body = payload or CopyBody()
    try:
        item = publish_clipboard.build_copy(
            layout_id, mode=body.mode or "", platform=body.platform or "",
            account_id=body.account_id, max_width=body.max_width or 0,
            quality=body.quality or 0)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"item": item}


# ---------------------------------------------------------------- 文章分发
class PublishBody(BaseModel):
    article_id: Optional[int] = None
    layout_id: Optional[int] = None
    account_id: Optional[int] = None
    account_ids: Optional[List[int]] = None
    draft_only: Optional[int] = 1
    title: Optional[str] = None


class FinishCleanupBody(BaseModel):
    ids: Optional[List[int]] = None
    all: bool = False


@router.post("/finish/cleanup")
def finish_cleanup(payload: FinishCleanupBody) -> Dict[str, Any]:
    """批量清理已交付成品（output_finish 目录 + 交付状态），文章与素材保留。"""
    ids = list(payload.ids or [])
    if payload.all or not ids:
        return finish.clear_all(None)
    return finish.clear_all(ids)


@router.delete("/finish/{article_id}")
def finish_clear_one(article_id: int) -> Dict[str, Any]:
    """清理单篇已交付成品（删目录、复位状态，可随时重新交付）。"""
    return finish.clear(article_id)


@router.post("/finish/{article_id}")
def finish_article(article_id: int) -> Dict[str, Any]:
    """重建交付目录。正常情况下改写完成、素材就绪会自动触发，这里是手动兜底。"""
    return finish.export_article(article_id)


@router.get("/finish/{article_id}")
def finish_state(article_id: int) -> Dict[str, Any]:
    item = finish.status(article_id)
    if not item:
        raise HTTPException(404, "文章不存在")
    return {"item": item}


@router.get("/publish/drivers")
def publish_drivers() -> Dict[str, Any]:
    """哪些平台能发、要不要浏览器。"""
    return {"items": publish.drivers()}


@router.post("/publish")
async def publish_article(payload: PublishBody) -> Dict[str, Any]:
    """把排版稿发到指定账号（可一次多个）。

    只入队并立刻返回任务号 —— 真实发布在后台 worker 里跑，前端轮询任务状态即可。
    """
    layout_id = payload.layout_id
    article_id = payload.article_id
    if layout_id:
        item = layout.detail(layout_id)
    elif article_id:
        item = layout.latest(article_id)
    else:
        raise HTTPException(400, "缺少 layout_id 或 article_id")
    if not item:
        raise HTTPException(404, "排版稿不存在，先排版再发布")
    article_id = int(item.get("article_id") or article_id or 0)

    ids = payload.account_ids or ([payload.account_id] if payload.account_id else [])
    if not ids:
        raise HTTPException(400, "请选择要发布到的账号")

    form = str(item.get("content_form") or "article")
    tasks = []
    for aid in ids:
        account = accounts.get_account(int(aid), include_cookie=True)
        if not account:
            tasks.append({"account_id": aid, "success": False, "error": "账号不存在"})
            continue
        if not account.get("enabled"):
            tasks.append({"account_id": aid, "account_name": account.get("name"),
                          "success": False, "error": "账号已停用"})
            continue
        # 关键：每个账号发「自己平台」的排版稿。以前所有账号共用一份，
        # 结果头条和百家发出同一篇稿子、同一个标题。
        plat = str(account.get("platform") or "")
        # 每个账号发自己平台、且形态受支持的版本：原文短时只有短稿（微头条 / 动态）
        acc_form = form if form in style.forms_of(plat) else "article"
        if not style.can_publish(plat, acc_form):
            # 例如百家号动态：成稿已交付，但网页端没有对应保存接口
            tasks.append({"account_id": aid, "account_name": account.get("name"),
                          "platform": plat, "content_form": acc_form, "success": False,
                          "error": f"{style.label_of(plat, acc_form)}暂不支持网页端自动发布，"
                                   f"成稿已交付到 output_finish，请到平台后台手动发"})
            continue
        lay = (layout.for_platform(article_id, plat, acc_form)
               if plat in style.PLATFORMS else item)
        if not lay:
            tasks.append({"account_id": aid, "account_name": account.get("name"),
                          "platform": plat, "success": False,
                          "error": f"这篇还没有{style.label_of(plat, acc_form)}版本，"
                                   f"先改写该版本再发"})
            continue
        task_id = publish_jobs.create_task(article_id, lay, account,
                                          bool(payload.draft_only),
                                          payload.title or "")
        await publish_jobs.submit(task_id)
        tasks.append({"account_id": aid, "account_name": account.get("name"),
                      "platform": plat, "content_form": acc_form, "layout_id": lay.get("id"),
                      "title": lay.get("title"), "task_id": task_id,
                      "success": True, "status": "pending"})
    return {"tasks": tasks, "count": len(tasks)}


@router.get("/publish/stats")
def publish_stats(days: int = 14) -> Dict[str, Any]:
    """发布数据统计：总量 / 账号维度 / 平台维度 / 近 N 天趋势（发布中心图表用）。"""
    return publish.stats(days)


@router.get("/publish/tasks")
def publish_tasks(article_id: Optional[int] = None, limit: int = 50) -> Dict[str, Any]:
    return {"items": publish.list_tasks(article_id, limit=max(1, min(500, limit)))}


@router.get("/publish/tasks/{task_id}")
def publish_task_detail(task_id: int) -> Dict[str, Any]:
    item = publish_jobs.get_task(task_id)
    if not item:
        raise HTTPException(404, "任务不存在")
    return {"item": item}


@router.post("/publish/tasks/{task_id}/retry")
async def publish_task_retry(task_id: int) -> Dict[str, Any]:
    try:
        return await publish_jobs.retry(task_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/publish/tasks/{task_id}/cancel")
def publish_task_cancel(task_id: int) -> Dict[str, Any]:
    try:
        return publish_jobs.cancel(task_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/publish/tasks/{task_id}")
def delete_publish_task(task_id: int) -> Dict[str, Any]:
    if not publish.delete_task(task_id):
        raise HTTPException(404, "任务不存在")
    return {"deleted": task_id}


@router.post("/accounts/{account_id}/test")
def test_account(account_id: int) -> Dict[str, Any]:
    """测登录态，顺带告诉前端这个平台能不能发。"""
    account = accounts.get_account(account_id, include_cookie=True)
    if not account:
        raise HTTPException(404, "账号不存在")
    driver = publish.get_driver(account)
    if not driver:
        raise HTTPException(400, f"平台 {account.get('platform')} 暂时没有分发驱动")
    try:
        auth = driver.check_auth()
    except Exception as exc:  # noqa: BLE001 - 驱动里的异常要变成人能看懂的话
        raise HTTPException(400, f"测登录失败：{exc}") from exc
    return {"ok": auth.ok, "auth": auth.to_dict(),
            "driver": {"name": driver.name, "needs_browser": driver.needs_browser}}


@router.post("/accounts/{account_id}/probe")
def probe_account(account_id: int) -> Dict[str, Any]:
    """头条这类没开源接口的平台：打开创作页抓真实请求，抓到的端点自动存库。"""
    account = accounts.get_account(account_id, include_cookie=True)
    if not account:
        raise HTTPException(404, "账号不存在")
    driver = publish.get_driver(account)
    if not driver or not hasattr(driver, "probe"):
        raise HTTPException(400, "该平台不需要探测")
    try:
        res = driver.probe()
    except Exception as exc:  # noqa: BLE001 - 别让用户只看到一个 Internal Server Error
        raise HTTPException(400, f"探测失败：{exc}") from exc
    if isinstance(res, list):          # 兼容老返回
        return {"items": res, "buckets": {}}
    buckets = res.get("buckets") or {}
    if any(buckets.values()):
        accounts.save_endpoints(account_id, buckets)
    return {"items": res.get("items") or [], "buckets": buckets,
            "saved": bool(any(buckets.values()))}


# ---------------------------------------------------------------- 发布账号
class AccountBody(BaseModel):
    platform: Optional[str] = None
    name: Optional[str] = None
    login_user: Optional[str] = None
    home_url: Optional[str] = None
    profile_dir: Optional[str] = None
    executable_path: Optional[str] = None
    headless: Optional[int] = None
    proxy: Optional[str] = None
    auto_refresh: Optional[int] = None
    refresh_hours: Optional[int] = None
    enabled: Optional[int] = None
    note: Optional[str] = None


@router.get("/accounts")
def list_accounts(platform: str = "") -> Dict[str, Any]:
    return {"items": accounts.list_accounts(platform), "meta": accounts.meta()}


@router.get("/accounts/meta")
def accounts_meta() -> Dict[str, Any]:
    return accounts.meta()


@router.post("/accounts")
def create_account(payload: AccountBody) -> Dict[str, Any]:
    try:
        new_id = accounts.upsert(payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": new_id, "item": accounts.get_account(new_id)}


@router.patch("/accounts/{account_id}")
def update_account(account_id: int, payload: AccountBody) -> Dict[str, Any]:
    try:
        accounts.upsert(payload.model_dump(exclude_none=True), account_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": account_id, "item": accounts.get_account(account_id)}


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, purge_profile: bool = True,
                   purge_tasks: bool = False) -> Dict[str, Any]:
    """删除账号。purge_profile 默认开：连带清掉浏览器 profile（登录态/缓存）。"""
    res = accounts.delete(account_id, purge_profile=purge_profile, purge_tasks=purge_tasks)
    if not res.get("deleted"):
        raise HTTPException(404, "账号不存在")
    return res


@router.post("/accounts/{account_id}/enable")
def enable_account(account_id: int, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    enabled = bool((payload or {}).get("enabled", True))
    item = accounts.set_enabled(account_id, enabled)
    if not item:
        raise HTTPException(404, "账号不存在")
    return {"id": account_id, "item": item}


@router.get("/accounts/{account_id}/refresh")
def account_refresh_state(account_id: int) -> Dict[str, Any]:
    return {"state": accounts.refresh_state(account_id),
            "item": accounts.get_account(account_id)}


@router.post("/accounts/{account_id}/refresh")
def account_refresh_start(account_id: int, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """开浏览器刷新 cookie。mode=manual 需人工登录后点保存；auto 直接无头续期。"""
    mode = str((payload or {}).get("mode") or "manual")
    try:
        return {"state": accounts.start_refresh(account_id, mode)}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/accounts/{account_id}/login")
def account_login_start(account_id: int) -> Dict[str, Any]:
    """扫码登录：无头打开登录页截二维码，前端轮询 refresh 状态拿 qr_path 展示。"""
    try:
        return {"state": accounts.start_login(account_id),
                "item": accounts.get_account(account_id)}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/accounts/{account_id}/login/qr")
def account_login_qr(account_id: int):
    """返回当前账号的二维码图片；没生成过返回 404，前端据此显示「等待二维码」。"""
    path = accounts.login_qr_path(account_id)
    if not path:
        raise HTTPException(404, "二维码还没生成，稍后再试")
    return FileResponse(path, media_type="image/png")


@router.post("/accounts/{account_id}/refresh/save")
def account_refresh_save(account_id: int) -> Dict[str, Any]:
    """手动刷新时，确认已登录后收 cookie。"""
    try:
        item = accounts.save_refresh(account_id)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"item": item, "state": accounts.refresh_state(account_id)}


@router.post("/accounts/{account_id}/refresh/cancel")
def account_refresh_cancel(account_id: int) -> Dict[str, Any]:
    return {"state": accounts.cancel_refresh(account_id),
            "item": accounts.get_account(account_id)}


@router.post("/accounts/{account_id}/cookie")
def import_account_cookie(account_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """手动粘贴 cookie：不开浏览器也能导入（WSLg 不可用 / 只想用本机 Chrome 时的兜底）。

    支持三种格式：JSON 数组、Netscape 文件、`a=1; b=2` 请求头串。
    """
    try:
        item = accounts.save_cookie_text(account_id, str(payload.get("cookie") or ""))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"item": item}


@router.get("/accounts/{account_id}/cookie")
def account_cookie(account_id: int) -> Dict[str, Any]:
    """取完整 cookie（只有这个接口回明文，页面默认不展示）。"""
    item = accounts.get_account(account_id, include_cookie=True)
    if not item:
        raise HTTPException(404, "账号不存在")
    return item


# ---------------------------------------------------------------- 账号数据
@router.post("/insight/collect/{account_id}")
def insight_collect(account_id: int,
                    payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """采集账号数据（只读）。耗时不短，前端要显示「采集中」。"""
    kinds = (payload or {}).get("kinds") or None
    try:
        return insight.collect_account(account_id, kinds)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/insight/overview")
def insight_overview(account_id: int = 0) -> Dict[str, Any]:
    return insight.overview(account_id)


# ---------------------------------------------------------------- 数据清理
class CleanupBody(BaseModel):
    target: str
    ids: Optional[List[int]] = None
    days: Optional[int] = 0
    statuses: Optional[List[str]] = None
    keep: Optional[int] = 1
    account_id: Optional[int] = 0


@router.get("/cleanup/scan")
def cleanup_scan() -> Dict[str, Any]:
    """先扫描：各项清理的条数与占用空间，页面用来显示「预计释放」。"""
    return cleanup.scan()


@router.post("/cleanup/run")
def cleanup_run(payload: CleanupBody) -> Dict[str, Any]:
    target = str(payload.target or "").strip()
    if target == "batches":
        res = cleanup.run_batches(payload.ids or [])
    elif target == "cache":
        res = cleanup.run_cache(payload.ids)
    elif target == "media":
        res = cleanup.run_media()
    elif target == "publish":
        res = cleanup.run_publish(payload.days or 0, payload.statuses,
                                  payload.account_id or 0)
    elif target == "rewrites":
        res = cleanup.run_rewrites(payload.keep or 1)
    elif target == "orphans":
        res = cleanup.run_orphans()
    elif target == "profiles":
        res = cleanup.run_profiles()
    elif target == "logs":
        res = cleanup.run_logs()
    else:
        raise HTTPException(400, f"未知清理项：{target}")
    return {"target": target, "result": res}


@router.post("/cleanup/vacuum")
def cleanup_vacuum() -> Dict[str, Any]:
    """收缩数据库文件（删了大量数据后执行）。"""
    return cleanup.vacuum()


# ---------------------------------------------------------------- 系统设置
@router.get("/settings")
def get_settings(category: str = "") -> Dict[str, Any]:
    return {"items": app_settings.list_settings(category),
            "categories": app_settings.CATEGORIES,
            # 提示词面板：平台块 → 阶段页签（前端按这个结构分组渲染）
            "prompt_groups": app_settings.prompt_groups()}


@router.put("/settings")
def put_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, dict):
        raise HTTPException(400, "缺少 items")
    try:
        saved = app_settings.save_settings(items)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    # 词库分级 / 白名单 / 追加词条改了：让下次校验用新配置重建
    if any(str(k).startswith("sensitive.") for k in items):
        sensitive.invalidate()
    return {"saved": saved, "items": app_settings.list_settings()}


@router.post("/settings/reset")
def reset_settings(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    keys = (payload or {}).get("keys")
    return {"reset": app_settings.reset_settings(keys if isinstance(keys, list) else None),
            "items": app_settings.list_settings()}


@router.post("/settings/probe")
def probe_setting(payload: Dict[str, Any]) -> Dict[str, Any]:
    """探测路径类配置：文件是否存在、可执行文件能否跑起来。"""
    payload = payload or {}
    key = str(payload.get("key") or "")
    value = str(payload.get("value") or "")
    prober = str(payload.get("prober") or "bin")
    if key and not value:
        value = app_settings.get_setting(key)
    return app_settings.probe_path(value, prober)


# ---------------------------------------------------------------- 大模型配置
@router.get("/llm-configs")
def list_llm_configs(category: str = "") -> Dict[str, Any]:
    return {"items": llm.list_configs(category)}


@router.get("/llm-meta")
def llm_meta() -> Dict[str, Any]:
    return llm.meta()


@router.post("/llm-configs")
def create_llm_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        new_id = llm.upsert(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": new_id, "item": llm.get_config(new_id)}


@router.patch("/llm-configs/{config_id}")
def update_llm_config(config_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        llm.upsert(payload, config_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": config_id, "item": llm.get_config(config_id)}


@router.delete("/llm-configs/{config_id}")
def delete_llm_config(config_id: int) -> Dict[str, Any]:
    if not llm.delete(config_id):
        raise HTTPException(404, "配置不存在")
    return {"deleted": config_id}


@router.post("/llm-configs/{config_id}/default")
def set_default_llm(config_id: int) -> Dict[str, Any]:
    try:
        llm.set_default(config_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": config_id, "item": llm.get_config(config_id)}


@router.post("/llm-configs/{config_id}/test")
def test_llm_config(config_id: int, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        return llm.test_config(config_id, payload or {})
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/llm-configs/test")
def test_llm_draft(payload: Dict[str, Any]) -> Dict[str, Any]:
    """未保存的配置也能先测连通（表单里的「测试连接」）。"""
    return llm.test_config(None, payload or {})


def _delete_cache_file(path: Optional[str]) -> None:
    if not path:
        return
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
    except OSError:
        pass
