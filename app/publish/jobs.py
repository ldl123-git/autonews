"""发布任务队列：API 只入队，worker 在后台串行执行。

解决三个老问题：
1. 发布不再占着 HTTP 请求（以前 6~15 秒/账号，账号多了必然超时）；
2. 进程重启留下的 running 任务会在启动时被回收成 failed（不再永久卡住）；
3. 失败可以重试、排队/运行中可以取消，同一个账号的任务串行执行避免抢 profile 锁。
"""
import asyncio
import datetime as dt
import time
from typing import Any, Dict, List, Optional

from ..db import get_conn, write_conn
from . import Article, count_videos, get_driver, keep_video, md_to_html

_QUEUE: Optional["asyncio.Queue[int]"] = None
_WORKERS: List["asyncio.Task[None]"] = []


def _q() -> "asyncio.Queue[int]":
    """惰性创建：模块导入时还没有运行中的事件循环，提前建 Queue 容易绑错 loop。"""
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = asyncio.Queue()
    return _QUEUE
_ACCOUNT_LOCKS: Dict[int, asyncio.Lock] = {}
CONCURRENCY = 2          # 同时跑几个账号（不同账号可并行，同账号串行）
HEARTBEAT_SECONDS = 5.0
STALE_MINUTES = 10       # 心跳断这么久就认为任务已经死了


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 任务行
def create_task(article_id: int, layout: Dict[str, Any], account: Dict[str, Any],
                draft_only: bool, title: str = "") -> int:
    """建一条 pending 任务，返回 task_id。"""
    form = str(layout.get("content_form") or "article")
    if form == "micro":
        title = ""                       # 微头条没有标题
        draft_only = True                # 网页端只支持存草稿
    else:
        title = title or str(layout.get("title") or "")
    # 能自己把视频传进稿子的平台（百家号）不算「跳过」，其余平台仍记待人工上传
    videos = (0 if keep_video(str(account.get("platform") or ""))
              else count_videos(str(layout.get("body_md") or "")))
    with write_conn() as conn:
        cur = conn.execute(
            """INSERT INTO publish_tasks
               (article_id, layout_id, account_id, platform, content_form, title, draft_only,
                status, image_count, video_skipped, attempts, max_attempts, heartbeat_at,
                cancel_requested, created_at)
               VALUES (?,?,?,?,?,?,?,'pending',0,?,0,3,'',0,?)""",
            (article_id, layout.get("id"), account.get("id"), account.get("platform"),
             form, title, 1 if draft_only else 0, videos, _now()))
        return int(cur.lastrowid or 0)


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM publish_tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def _mark(task_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with write_conn() as conn:
        conn.execute(f"UPDATE publish_tasks SET {sets} WHERE id=?",
                     [*fields.values(), task_id])


def _finish(task_id: int, status: str, *, post_id: str = "", post_url: str = "",
            error: str = "", image_count: Optional[int] = None,
            elapsed: Optional[float] = None) -> None:
    fields: Dict[str, Any] = {"status": status, "error": (error or "")[:500],
                              "finished_at": _now(), "heartbeat_at": _now()}
    if post_id:
        fields["post_id"] = post_id
    if post_url:
        fields["post_url"] = post_url
    if image_count is not None:
        fields["image_count"] = image_count
    if elapsed is not None:
        fields["elapsed"] = round(elapsed, 1)
    _mark(task_id, **fields)


# ---------------------------------------------------------------- 恢复 / 入队
def recover_stale(minutes: int = STALE_MINUTES) -> int:
    """把心跳早就断掉的 running 任务标成失败（进程重启 / 崩溃留下的僵尸）。"""
    cutoff = (dt.datetime.now() - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with write_conn() as conn:
        cur = conn.execute(
            "UPDATE publish_tasks SET status='failed', finished_at=?, heartbeat_at=?,"
            " error=COALESCE(NULLIF(error,''), '任务被中断（服务重启或超时），可以重试')"
            " WHERE status='running' AND (heartbeat_at IS NULL OR heartbeat_at=''"
            " OR heartbeat_at < ?)", (_now(), _now(), cutoff))
        return max(0, cur.rowcount or 0)


async def submit(task_id: int) -> None:
    """入队（API 是 async 路由，直接 put 到当前事件循环的队列）。"""
    _q().put_nowait(int(task_id))


async def retry(task_id: int) -> Dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise LookupError("任务不存在")
    if str(task.get("status")) not in ("failed", "cancelled", "interrupted"):
        raise ValueError("只有失败 / 已取消的任务可以重试")
    _mark(task_id, status="pending", error="", cancel_requested=0, finished_at="",
          heartbeat_at="", attempts=int(task.get("attempts") or 0) + 1)
    _q().put_nowait(int(task_id))
    return {"task_id": task_id, "status": "pending"}


def cancel(task_id: int) -> Dict[str, Any]:
    """取消：排队中的直接标取消；运行中的只能打标记，等它结束。"""
    task = get_task(task_id)
    if not task:
        raise LookupError("任务不存在")
    status = str(task.get("status"))
    if status == "pending":
        _finish(task_id, "cancelled", error="已取消")
        return {"task_id": task_id, "status": "cancelled"}
    if status == "running":
        _mark(task_id, cancel_requested=1)
        return {"task_id": task_id, "status": "cancelling"}
    raise ValueError("这条任务已经结束了，不能取消")


# ---------------------------------------------------------------- 执行
def _run_sync(task: Dict[str, Any]) -> Any:
    """在 worker 线程里跑一次真实发布（playwright 是同步 API）。"""
    from .. import accounts
    from ..layout import detail as layout_detail

    account = accounts.get_account(int(task["account_id"]), include_cookie=True)
    if not account:
        raise LookupError("账号已不存在")
    layout = layout_detail(int(task["layout_id"])) if task.get("layout_id") else None
    if not layout:
        raise LookupError("排版稿已不存在，重新排版后再发")
    driver = get_driver(account)
    if not driver:
        raise LookupError(f"平台 {account.get('platform')} 还没有分发驱动")
    form = str(task.get("content_form") or layout.get("content_form") or "article")
    title = "" if form == "micro" else str(task.get("title") or layout.get("title") or "")
    art = Article(title=title, html=md_to_html(str(layout.get("body_md") or ""),
                                               keep_video=keep_video(account.get("platform"))),
                  markdown=str(layout.get("body_md") or ""), form=form)
    draft_only = True if form == "micro" else bool(task.get("draft_only"))
    return driver.publish(art, draft_only=draft_only)


async def _heartbeat(task_id: int) -> None:
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                _mark(task_id, heartbeat_at=_now())
            except Exception:  # noqa: BLE001 - 心跳失败不该中断任务
                pass
    except asyncio.CancelledError:
        return


async def _execute(task_id: int) -> None:
    task = get_task(task_id)
    if not task:
        return
    if int(task.get("cancel_requested") or 0):
        _finish(task_id, "cancelled", error="已取消")
        return

    account_id = int(task.get("account_id") or 0)
    lock = _ACCOUNT_LOCKS.setdefault(account_id, asyncio.Lock())
    async with lock:                       # 同一账号串行，避免抢 profile 单例锁
        started = time.time()
        _mark(task_id, status="running", heartbeat_at=_now(), error="")
        hb = asyncio.create_task(_heartbeat(task_id))
        try:
            res = await asyncio.to_thread(_run_sync, task)
            if getattr(res, "success", False):
                _finish(task_id, "success",
                        post_id=str(getattr(res, "post_id", "") or ""),
                        post_url=str(getattr(res, "post_url", "") or ""),
                        error="", elapsed=time.time() - started)
            else:
                _finish(task_id, "failed",
                        post_id=str(getattr(res, "post_id", "") or ""),
                        error=str(getattr(res, "error", "") or "发布失败"),
                        elapsed=time.time() - started)
        except Exception as exc:  # noqa: BLE001 - 任务失败只影响自己
            _finish(task_id, "failed", error=f"{type(exc).__name__}: {exc}",
                    elapsed=time.time() - started)
        finally:
            hb.cancel()
            # 运行期间点过取消：结束后把状态改成 cancelled，别让人以为发成功了
            if int((get_task(task_id) or {}).get("cancel_requested") or 0):
                _mark(task_id, status="cancelled", error="运行中被取消（可能已提交到平台，请到后台确认）")


async def _worker() -> None:
    while True:
        task_id = await _q().get()
        try:
            await _execute(int(task_id))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        finally:
            _q().task_done()


async def start() -> None:
    """服务启动时调用：回收僵尸任务 + 拉起 worker。"""
    _q()                        # 先在事件循环里把队列建好
    recover_stale()
    if _WORKERS:
        return
    for _ in range(max(1, CONCURRENCY)):
        _WORKERS.append(asyncio.create_task(_worker()))


async def stop() -> None:
    for w in _WORKERS:
        w.cancel()
    _WORKERS.clear()
