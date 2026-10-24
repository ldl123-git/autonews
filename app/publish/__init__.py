"""文章分发：把排版稿发到各平台账号的草稿箱。

驱动契约照搬 Wechatsync 的适配器（checkAuth / uploadFile / publish + SyncResult），
百家号走纯 HTTP，头条走浏览器页面内请求（有反爬签名）。
"""
from typing import Any, Dict, List, Optional

from .base import Article, AuthResult, PlatformDriver, SyncResult, count_videos, md_to_html
from .baijiahao import BaijiahaoDriver
from .toutiao import ToutiaoDriver

__all__ = [
    "Article", "AuthResult", "SyncResult", "PlatformDriver",
    "BaijiahaoDriver", "ToutiaoDriver", "count_videos", "md_to_html",
    "drivers", "get_driver", "keep_video", "run", "list_tasks", "delete_task",
]

_DRIVERS = {d.id: d for d in (BaijiahaoDriver, ToutiaoDriver)}


def drivers() -> List[Dict[str, Any]]:
    """给前端用的驱动清单：哪些平台能发、要不要浏览器。"""
    out = []
    for key, cls in _DRIVERS.items():
        out.append({"key": key, "name": cls.name, "homepage": cls.homepage,
                    "needs_browser": cls.needs_browser,
                    "output_format": cls.output_format})
    return out


def get_driver(account: Dict[str, Any]) -> Optional[PlatformDriver]:
    cls = _DRIVERS.get(str(account.get("platform") or ""))
    return cls(account) if cls else None


def keep_video(platform: str) -> bool:
    """正文里的视频是否交给驱动自己上传并插进稿子（目前只有百家号支持）。

    百家号的视频上传链路是照编辑器前端 bundle 实现的（见 baijiahao 文件头）；
    头条的写接口带反爬签名且视频要先进自家视频库，仍然只能人工插。
    """
    return str(platform or "") == "baijia"


def run(article_id: int, layout: Dict[str, Any], account: Dict[str, Any],
        draft_only: bool = True, title: str = "") -> Dict[str, Any]:
    """执行一次分发，并把结果落到 publish_tasks。"""
    from ..db import write_conn

    driver = get_driver(account)
    if not driver:
        raise LookupError(f"平台 {account.get('platform')} 还没有对应的分发驱动")

    form = str(layout.get("content_form") or "article")
    title = "" if form == "micro" else (title or layout.get("title") or "")
    if form == "micro":
        draft_only = True                    # 微头条网页端只支持存草稿
    html = md_to_html(layout.get("body_md") or "",
                      keep_video=keep_video(account.get("platform")))
    art = Article(title=title, html=html, markdown=layout.get("body_md") or "", form=form)
    videos = (0 if keep_video(account.get("platform"))
              else count_videos(layout.get("body_md") or ""))

    started = _now_float()
    with write_conn() as conn:
        cur = conn.execute(
            """INSERT INTO publish_tasks
               (article_id, layout_id, account_id, platform, content_form, title, draft_only,
                status, image_count, video_skipped, created_at)
               VALUES (?,?,?,?,?,?,?,'running',0,?,?)""",
            (article_id, layout.get("id"), account.get("id"), account.get("platform"),
             form, title, 1 if draft_only else 0, videos, _now()))
        task_id = int(cur.lastrowid or 0)

    result = driver.publish(art, draft_only=draft_only)

    with write_conn() as conn:
        conn.execute(
            """UPDATE publish_tasks SET status=?, post_id=?, post_url=?, error=?,
               image_count=?, elapsed=?, finished_at=? WHERE id=?""",
            ("success" if result.success else "failed", result.post_id, result.post_url,
             "" if result.success else result.error,
             _img_count(result.message), round(_now_float() - started, 1),
             _now(), task_id))
    return {"task_id": task_id, "success": result.success, "post_url": result.post_url,
            "error": result.error, "message": result.message, "video_skipped": videos}


def list_tasks(article_id: Optional[int] = None, limit: int = 50) -> List[Dict[str, Any]]:
    from ..db import get_conn
    where = " WHERE article_id=?" if article_id else ""
    args: List[Any] = [article_id] if article_id else []
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT t.*, a.name AS account_name, a.platform AS account_platform
                FROM publish_tasks t LEFT JOIN publish_accounts a ON a.id=t.account_id
                {where} ORDER BY t.id DESC LIMIT ?""",
            [*args, limit]).fetchall()
    return [dict(r) for r in rows]


def delete_task(task_id: int) -> bool:
    from ..db import write_conn
    with write_conn() as conn:
        cur = conn.execute("DELETE FROM publish_tasks WHERE id=?", (task_id,))
    return bool(cur.rowcount)


def stats(days: int = 14) -> Dict[str, Any]:
    """发布数据统计：总量 / 账号维度 / 平台维度 / 近 N 天趋势。

    账号维度的目的是把「发布记录」和「账号」真正关联起来：
    哪个账号发了多少、成功多少、失败多少、最后一次是什么时候。
    """
    import datetime as dt
    from ..db import get_conn

    days = max(1, min(90, int(days or 14)))
    cutoff = (dt.datetime.now() - dt.timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00:00")
    with get_conn() as conn:
        def rows(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

        by_status = {str(r["status"]): int(r["c"]) for r in rows(
            "SELECT status, COUNT(*) c FROM publish_tasks GROUP BY status")}
        by_account = rows(
            """SELECT t.account_id AS id, a.name AS name, a.platform AS account_platform,
                      t.platform AS platform, a.enabled AS account_enabled,
                      COUNT(*) AS total,
                      SUM(CASE WHEN t.status='success' THEN 1 ELSE 0 END) AS success,
                      SUM(CASE WHEN t.status='failed' THEN 1 ELSE 0 END) AS failed,
                      SUM(CASE WHEN t.status IN ('pending','running') THEN 1 ELSE 0 END) AS running,
                      MAX(t.created_at) AS last_at
               FROM publish_tasks t LEFT JOIN publish_accounts a ON a.id = t.account_id
               GROUP BY t.account_id ORDER BY total DESC""")
        for a in by_account:
            a["name"] = a.get("name") or (f"账号#{a.get('id')}" if a.get("id") else "未知账号")
            a["platform"] = a.get("account_platform") or a.get("platform") or ""
            a["deleted"] = 0 if a.get("account_enabled") is not None else 1
        by_platform = rows(
            """SELECT COALESCE(NULLIF(platform,''), '-') AS platform,
                      COALESCE(NULLIF(content_form,''),'article') AS content_form,
                      COUNT(*) AS total,
                      SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
               FROM publish_tasks GROUP BY 1, 2 ORDER BY total DESC""")
        daily = rows(
            """SELECT substr(created_at,1,10) AS date, COUNT(*) AS total,
                      SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
               FROM publish_tasks WHERE created_at >= ? GROUP BY 1 ORDER BY 1""", (cutoff,))
    total = sum(by_status.values())
    ok = by_status.get("success", 0)
    return {
        "total": total,
        "by_status": by_status,
        "success_rate": round(ok / total, 4) if total else 0,
        "by_account": by_account,
        "by_platform": by_platform,
        "daily": daily,
        "days": days,
    }


def _img_count(message: str) -> int:
    import re
    m = re.search(r"上传图片\s*(\d+)\s*张", str(message or ""))
    return int(m.group(1)) if m else 0


def _now() -> str:
    import datetime as dt
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_float() -> float:
    import time
    return time.time()
