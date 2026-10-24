"""数据清理中心：先扫描（条数 + 占用）再执行，删除范围要能说清楚。

原则：
* 一次清理只开一个写事务，避免删一半；
* 磁盘删除必须落在白名单根目录内（防 output_dir 被改成 ../..）；
* 每项返回 rows / files / bytes，页面上显示「预计释放 X MB」；
* 大量删除后要 VACUUM，否则 app.db 不会变小。
"""
import datetime as dt
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import CACHE_DIR, DATA_DIR, FINISH_DIR, OUTPUT_DIR
from .db import get_conn, write_conn

#: 允许被清理模块删除的根目录。任何不在这些目录下的路径一律拒绝。
_SAFE_ROOTS = (OUTPUT_DIR, FINISH_DIR, CACHE_DIR, DATA_DIR / "media")


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _under_safe_root(path: Path) -> bool:
    """路径必须落在白名单根目录内部（不是根本身）。"""
    try:
        target = path.resolve()
    except OSError:
        return False
    for root in _SAFE_ROOTS:
        try:
            r = root.resolve()
        except OSError:
            continue
        if target != r and r in target.parents:
            return True
    return False


def _rmtree(path: Path) -> int:
    """安全删除目录，返回释放字节数。不在白名单内直接拒绝。"""
    if not path.exists() or not _under_safe_root(path):
        return 0
    size = _dir_size(path)
    shutil.rmtree(path, ignore_errors=True)
    return size


def _unlink(path: Path) -> int:
    try:
        if path.is_file():
            size = path.stat().st_size
            path.unlink()
            return size
    except OSError:
        pass
    return 0


def _files_under(folder: Path) -> List[Path]:
    if not folder.exists():
        return []
    return [p for p in folder.rglob("*") if p.is_file()]


# ---------------------------------------------------------------- 扫描
def _scan_batches() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT b.id, b.filename, b.status, b.total, b.created_at,"
            " (SELECT COUNT(*) FROM articles a WHERE a.batch_id=b.id) AS articles"
            " FROM batches b ORDER BY b.id DESC LIMIT 200").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["bytes"] = 0
        out.append(d)
    return out


def _scan_cache() -> Dict[str, Any]:
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM raw_cache").fetchone()["c"]
    files = _files_under(CACHE_DIR)
    return {"rows": n, "files": len(files), "bytes": sum(_safe_size(p) for p in files)}


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _scan_media() -> Dict[str, Any]:
    """无主素材：库里记录的文章已不存在，或状态不是 ready。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.id, m.path, m.bytes FROM media_assets m"
            " LEFT JOIN articles a ON a.id=m.article_id"
            " WHERE a.id IS NULL OR m.status<>'ready'").fetchall()
    used = set()
    with get_conn() as conn:
        used = {str(r["path"] or "") for r in conn.execute(
            "SELECT path FROM media_assets WHERE status='ready'").fetchall()}
    orphans = [p for p in _files_under(DATA_DIR / "media")
               if str(p) not in used and p.as_posix() not in used]
    return {"rows": len(rows), "files": len(orphans),
            "bytes": sum(int(r["bytes"] or 0) for r in rows)
                     + sum(_safe_size(p) for p in orphans)}


def _scan_publish() -> Dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) c FROM publish_tasks GROUP BY status").fetchall()
    stats = {str(r["status"]): int(r["c"]) for r in rows}
    return {"total": sum(stats.values()), "by_status": stats}


def _scan_rewrite() -> Dict[str, Any]:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM article_rewrites").fetchone()["c"]
        stale = conn.execute(
            "SELECT COUNT(*) c FROM article_rewrites WHERE status<>'success'").fetchone()["c"]
        layouts = conn.execute(
            "SELECT COUNT(*) c FROM article_layouts WHERE status='draft'").fetchone()["c"]
    return {"rewrites": total, "failed_rewrites": stale, "draft_layouts": layouts}


def _scan_orphan_dirs() -> Dict[str, Any]:
    """磁盘上有、库里没引用的输出目录。

    只扫到「文章目录」这一层：output/<标题> 与 output_finish/<平台>/<标题>。
    平台目录（tt / bjh）本身永远不算孤儿，否则一键清理会把整个平台目录删掉。
    """
    with get_conn() as conn:
        used_out = {str(r["output_dir"] or "").replace("\\", "/")
                    for r in conn.execute(
                        "SELECT output_dir FROM articles WHERE output_dir<>''").fetchall()}
        used_finish = set()
        for r in conn.execute(
                "SELECT finish_dir FROM articles WHERE finish_dir<>''").fetchall():
            for part in str(r["finish_dir"] or "").split(","):
                if part.strip():
                    used_finish.add(part.strip().replace("\\", "/"))
    items: List[Dict[str, Any]] = []
    if OUTPUT_DIR.exists():
        for d in OUTPUT_DIR.iterdir():
            if not d.is_dir() or d.name in used_out:
                continue
            items.append({"path": str(d), "name": f"output/{d.name}",
                          "bytes": _dir_size(d)})
    if FINISH_DIR.exists():
        for plat in FINISH_DIR.iterdir():
            if not plat.is_dir():
                continue
            for d in plat.iterdir():
                if not d.is_dir():
                    continue
                rel = f"{plat.name}/{d.name}"
                if rel in used_finish:
                    continue
                items.append({"path": str(d), "name": f"output_finish/{rel}",
                              "bytes": _dir_size(d)})
    return {"count": len(items), "bytes": sum(i["bytes"] for i in items), "items": items}


def _scan_profiles() -> Dict[str, Any]:
    """浏览器 profile 里没有对应账号的目录（删账号没清干净留下的）。"""
    from .accounts import profile_root
    root = profile_root()
    with get_conn() as conn:
        used = {str(r["profile_dir"] or "").rstrip("/")
                for r in conn.execute("SELECT profile_dir FROM publish_accounts").fetchall()}
    items = []
    if root.exists():
        for d in root.iterdir():
            if d.is_dir() and str(d).rstrip("/") not in used:
                items.append({"path": str(d), "name": d.name, "bytes": _dir_size(d)})
    return {"count": len(items), "bytes": sum(i["bytes"] for i in items), "items": items}


def _scan_logs() -> Dict[str, Any]:
    files = sorted(DATA_DIR.glob("*.log"))
    return {"count": len(files),
            "bytes": sum(_safe_size(p) for p in files),
            "items": [{"name": p.name, "bytes": _safe_size(p)} for p in files]}


def scan() -> Dict[str, Any]:
    """一次性给出所有清理项的现状，页面据此展示「预计释放」。"""
    batches = _scan_batches()
    cache = _scan_cache()
    media = _scan_media()
    publish = _scan_publish()
    rewrite = _scan_rewrite()
    orphans = _scan_orphan_dirs()
    profiles = _scan_profiles()
    logs = _scan_logs()
    return {
        "scanned_at": _now(),
        "batches": {"count": len(batches), "items": batches},
        "cache": cache,
        "media": media,
        "publish": publish,
        "rewrite": rewrite,
        "orphans": orphans,
        "profiles": profiles,
        "logs": logs,
        "total_bytes": sum(x.get("bytes", 0) for x in
                           (cache, media, orphans, profiles, logs)),
    }


# ---------------------------------------------------------------- 执行
def _purge_article(article_id: int) -> int:
    """删一篇的全部产物：缓存文件、素材文件、改写/排版记录、交付目录、文章行。"""
    freed = 0
    with get_conn() as conn:
        cache_paths = [str(r["path"] or "") for r in conn.execute(
            "SELECT path FROM raw_cache WHERE article_id=?", (article_id,)).fetchall()]
        media = [dict(r) for r in conn.execute(
            "SELECT id, path FROM media_assets WHERE article_id=?", (article_id,)).fetchall()]
        row = conn.execute(
            "SELECT output_dir, finish_dir FROM articles WHERE id=?", (article_id,)).fetchone()
    for p in cache_paths:
        if p:
            freed += _unlink(Path(p))
    for m in media:
        path = str(m.get("path") or "")
        if path:
            freed += _unlink(Path(path))
    if row:
        out_dir = str(row["output_dir"] or "").strip()
        if out_dir:
            freed += _rmtree(OUTPUT_DIR / out_dir)
        for part in str(row["finish_dir"] or "").split(","):
            part = part.strip()
            if part:
                freed += _rmtree(FINISH_DIR / part)
    with write_conn() as conn:
        conn.execute("DELETE FROM media_assets WHERE article_id=?", (article_id,))
        conn.execute("DELETE FROM raw_cache WHERE article_id=?", (article_id,))
        conn.execute("DELETE FROM article_layouts WHERE article_id=?", (article_id,))
        conn.execute("DELETE FROM article_rewrites WHERE article_id=?", (article_id,))
        conn.execute("DELETE FROM articles WHERE id=?", (article_id,))
    return freed


def run_batches(batch_ids: List[int]) -> Dict[str, Any]:
    """删数据源表格：批次 + 该批全部文章及级联产物。"""
    ids = [int(i) for i in (batch_ids or []) if i]
    if not ids:
        return {"batches": 0, "articles": 0, "freed_bytes": 0}
    q = ",".join("?" * len(ids))
    with get_conn() as conn:
        arts = [int(r["id"]) for r in conn.execute(
            f"SELECT id FROM articles WHERE batch_id IN ({q})", ids).fetchall()]
    freed = sum(_purge_article(a) for a in arts)
    with write_conn() as conn:
        conn.execute(f"DELETE FROM batches WHERE id IN ({q})", ids)
    return {"batches": len(ids), "articles": len(arts), "freed_bytes": freed}


def run_cache(article_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """清空采样缓存；对应文章的抓取状态复位为 pending，可以重新抓。"""
    ids = [int(i) for i in (article_ids or []) if i]
    with get_conn() as conn:
        if ids:
            q = ",".join("?" * len(ids))
            rows = [dict(r) for r in conn.execute(
                f"SELECT id, path FROM raw_cache WHERE article_id IN ({q})", ids).fetchall()]
        else:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, path FROM raw_cache").fetchall()]
    freed = sum(_unlink(Path(str(r["path"] or ""))) for r in rows if r["path"])
    with write_conn() as conn:
        if ids:
            q = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM raw_cache WHERE article_id IN ({q})", ids)
            conn.execute(f"UPDATE articles SET content_id=NULL, fetch_status='pending',"
                         f" fetch_error='' WHERE id IN ({q})", ids)
        else:
            conn.execute("DELETE FROM raw_cache")
            conn.execute("UPDATE articles SET content_id=NULL, fetch_status='pending',"
                         " fetch_error=''")
    return {"rows": len(rows), "freed_bytes": freed}


def run_media() -> Dict[str, Any]:
    """删无主素材：文章已不存在 / 非 ready 的记录，以及没被引用的素材文件。"""
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT m.id, m.path FROM media_assets m LEFT JOIN articles a ON a.id=m.article_id"
            " WHERE a.id IS NULL OR m.status<>'ready'").fetchall()]
        used = {str(r["path"] or "") for r in conn.execute(
            "SELECT path FROM media_assets WHERE status='ready'").fetchall()}
    freed = 0
    for r in rows:
        freed += _unlink(Path(str(r["path"] or "")))
    for p in _files_under(DATA_DIR / "media"):
        if str(p) not in used and p.as_posix() not in used:
            freed += _unlink(p)
    with write_conn() as conn:
        ids = [int(r["id"]) for r in rows]
        if ids:
            q = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM media_assets WHERE id IN ({q})", ids)
        conn.execute("DELETE FROM media_assets WHERE article_id NOT IN (SELECT id FROM articles)")
    return {"rows": len(rows), "freed_bytes": freed}


def run_publish(days: int = 0, statuses: Optional[List[str]] = None,
                account_id: int = 0) -> Dict[str, Any]:
    """删发布记录：按时间窗 / 状态 / 账号筛，留空则全删。"""
    clauses, args = [], []
    if int(days or 0) > 0:
        clauses.append("created_at < ?")
        args.append((dt.datetime.now() - dt.timedelta(days=int(days)))
                    .strftime("%Y-%m-%d %H:%M:%S"))
    if statuses:
        clauses.append(f"status IN ({','.join('?' * len(statuses))})")
        args += list(statuses)
    if int(account_id or 0):
        clauses.append("account_id = ?")
        args.append(int(account_id))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with write_conn() as conn:
        cur = conn.execute(f"DELETE FROM publish_tasks{where}", args)
        removed = max(0, cur.rowcount or 0)
    return {"rows": removed}


def run_rewrites(keep: int = 1) -> Dict[str, Any]:
    """改写历史瘦身：每个(文章, 平台, 形态)只留最新 keep 条；草稿排版全删。"""
    keep = max(1, int(keep or 1))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, article_id, target_platform,"
            " COALESCE(NULLIF(content_form,''),'article') AS form"
            " FROM article_rewrites"
            " ORDER BY article_id, target_platform, form, id DESC").fetchall()
    seen: Dict[Any, int] = {}
    drop: List[int] = []
    for r in rows:
        key = (int(r["article_id"] or 0), str(r["target_platform"] or ""), str(r["form"]))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > keep:
            drop.append(int(r["id"]))
    with write_conn() as conn:
        if drop:
            q = ",".join("?" * len(drop))
            conn.execute(f"DELETE FROM article_rewrites WHERE id IN ({q})", drop)
        cur = conn.execute("DELETE FROM article_layouts WHERE status='draft'")
        removed = len(drop) + max(0, cur.rowcount or 0)
    return {"rows": removed, "rewrites": len(drop)}


def run_orphans() -> Dict[str, Any]:
    """删磁盘上有、库里没引用的输出目录。"""
    info = _scan_orphan_dirs()
    freed = 0
    for it in info.get("items") or []:
        freed += _rmtree(Path(it["path"]))
    return {"rows": int(info.get("count") or 0), "freed_bytes": freed}


def run_profiles() -> Dict[str, Any]:
    """删无主 profile 目录。必须落在 profile_root 内部，防止误删。"""
    from .accounts import profile_root
    info = _scan_profiles()
    try:
        root = profile_root().resolve()
    except OSError:
        return {"rows": 0, "freed_bytes": 0}
    freed, removed = 0, 0
    for it in info.get("items") or []:
        try:
            target = Path(it["path"]).resolve()
        except OSError:
            continue
        if target == root or root not in target.parents:
            continue
        freed += _dir_size(target)
        shutil.rmtree(target, ignore_errors=True)
        removed += 1
    return {"rows": removed, "freed_bytes": freed}


def run_logs() -> Dict[str, Any]:
    """日志轮转：超过 5 MB 的清空（保留文件本身，方便继续追加）。"""
    freed = 0
    for p in DATA_DIR.glob("*.log"):
        size = _safe_size(p)
        if size <= 5 * 1024 * 1024:
            continue
        try:
            p.write_text("", encoding="utf-8")
            freed += size
        except OSError:
            continue
    return {"rows": 0, "freed_bytes": freed}


def vacuum() -> Dict[str, Any]:
    """收缩数据库文件。删了大量行之后必须做，否则 app.db 不会变小。"""
    import sqlite3
    from .config import DB_PATH
    before = _safe_size(DB_PATH)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = _safe_size(DB_PATH)
    return {"before": before, "after": after, "freed_bytes": max(0, before - after)}
