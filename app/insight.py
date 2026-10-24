"""账号数据采集：作品列表 / 账号概览 / 粉丝分布。

只读：都在账号自己的浏览器 profile 里，用页面内请求打平台统计接口，
不碰任何写接口。采集失败只影响数据看板，绝不影响发布链路。
"""
import datetime as dt
import json
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from . import accounts
from .db import get_conn, write_conn

#: 各平台的数据页（当前只做头条；百家号页面结构差异大，先留空）
PAGES: Dict[str, Dict[str, str]] = {
    "toutiao": {
        "works": "https://mp.toutiao.com/profile_v4/manage/content/all",
        "overview": "https://mp.toutiao.com/profile_v4/analysis/works-overall/all",
        "fans": "https://mp.toutiao.com/profile_v4/analysis/fans/overview",
    },
}

_API_HINTS = ("/pgc/ma/", "/mp/agw/", "statistic", "analysis", "content", "fans")
#: 账号信息接口：页面接口抓不到时的兜底（这个端点发布驱动已在用，比较稳）
_USER_API = "https://mp.toutiao.com/mp/agw/creator_center/user_info"
#: 埋点 / AB 实验的 JSON 一律不要，否则快照里全是噪声
_BLOCK_HOSTS = ("mcs.zijieapi.com", "abtestvm.bytedance.com", "slardar",
                "bytedapm", "bytegoofy")


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def _session(account: Dict[str, Any]) -> Iterator[Any]:
    """打开账号的持久化上下文，用完一定关掉。"""
    from playwright.sync_api import sync_playwright

    from .publish.base import clear_profile_locks, has_running_browser, in_use_error

    if accounts.refresh_state(int(account["id"])).get("running"):
        raise RuntimeError("这个账号正在登录 / 刷新，等它结束再采集数据")
    profile = accounts._profile_dir(account)
    if not profile:
        raise RuntimeError("账号没有可用的浏览器数据目录，先扫码登录一次")

    pw = sync_playwright().start()
    ctx = None
    try:
        kwargs = accounts._browser_kwargs(account)
        kwargs["headless"] = True
        try:
            ctx = pw.chromium.launch_persistent_context(str(profile), **kwargs)
        except Exception as exc:  # noqa: BLE001
            if in_use_error(exc) and not has_running_browser(profile):
                clear_profile_locks(profile)
                ctx = pw.chromium.launch_persistent_context(str(profile), **kwargs)
            else:
                raise
        yield ctx
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass


def _collect_page(ctx: Any, url: str, seconds: float = 8.0) -> List[Any]:
    """打开数据页，把页面自己发的相关 JSON 响应都收下来（不改页面）。

    头条后台是 SPA：首屏先加载一堆 JS，业务接口要 6~10 秒才发出去，
    等待时间给短了会一条都抓不到（实测 4 秒时命中 0，8 秒起才有）。
    """
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    captured: List[Any] = []

    def on_response(resp: Any) -> None:
        try:
            u = resp.url
            if any(h in u for h in _BLOCK_HOSTS):
                return
            if "json" not in (resp.headers or {}).get("content-type", ""):
                return
            if "mp.toutiao.com" not in u and not any(h in u for h in _API_HINTS):
                return
            captured.append(resp.json())
        except Exception:  # noqa: BLE001 - 单个响应解析失败忽略
            return

    page.on("response", on_response)
    try:
        page.goto(url, timeout=60_000, wait_until="domcontentloaded")
        page.wait_for_timeout(int(seconds * 1000))
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2500)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:  # noqa: BLE001
            pass
    return captured


def _fetch_json(page: Any, url: str) -> Any:
    """在页面里直接发一个 GET（带登录态），拿不到就返回 None。"""
    try:
        return page.evaluate(
            """async (url) => {
                try {
                    const r = await fetch(url, { credentials: 'include' });
                    const t = await r.text();
                    try { return JSON.parse(t); } catch (e) { return null; }
                } catch (e) { return null; }
            }""", url)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 解析
def _num(value: Any) -> int:
    """把 "1.2万" / "1,234" / 1234 统一成整数。"""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value or "").strip().replace(",", "")
    if not s:
        return 0
    mul = 1
    if s.endswith(("万", "w", "W")):
        mul, s = 10000, s[:-1]
    elif s.endswith(("千", "k", "K")):
        mul, s = 1000, s[:-1]
    digits = "".join(ch for ch in s if ch.isdigit() or ch == ".")
    try:
        return int(float(digits) * mul)
    except ValueError:
        return 0


def _pick(node: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in node and node[k] not in (None, ""):
            return node[k]
    return ""


def _extract_items(payloads: List[Any]) -> List[Dict[str, Any]]:
    """从各种嵌套结构里捞出「像作品」的对象（有 title 或 item_id）。"""
    out: List[Dict[str, Any]] = []
    seen = set()

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 4 or len(out) > 500:
            return
        if isinstance(node, list):
            for x in node[:100]:
                if isinstance(x, dict) and (_pick(x, "title", "article_title")
                                            or _pick(x, "item_id", "gid_str", "id")):
                    key = str(_pick(x, "item_id", "gid_str", "id")) + "|" + str(
                        _pick(x, "title", "article_title"))
                    if key not in seen:
                        seen.add(key)
                        out.append(x)
                elif isinstance(x, (dict, list)):
                    walk(x, depth + 1)
        elif isinstance(node, dict):
            for k in ("data", "list", "items", "content", "result", "works", "items_list"):
                if k in node:
                    walk(node[k], depth + 1)

    for p in payloads:
        walk(p)
    return out


def _account_metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    """账号信息接口 → 看板用的几个指标（各版本字段名不一致，模糊取）。"""
    d = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(d, dict):
        return {}

    def pick(*keys: str) -> Any:
        for k in keys:
            v = d.get(k)
            if v not in (None, "", []):
                return v
        return ""

    return {
        "name": str(pick("name", "nickname", "media_name", "screen_name")),
        "media_id": str(pick("media_id", "user_id", "uid", "id")),
        "avatar": str(pick("avatar_url", "avatar", "large_avatar_url")),
        "fans": _num(pick("fans_count", "follower_count", "fans", "total_fans")),
    }


def _save_snapshot(account_id: int, platform: str, kind: str, payloads: List[Any]) -> int:
    with write_conn() as conn:
        cur = conn.execute(
            "INSERT INTO insight_snapshots (account_id, platform, kind, payload, captured_at)"
            " VALUES (?,?,?,?,?)",
            (account_id, platform, kind,
             json.dumps(payloads, ensure_ascii=False)[:2_000_000], _now()))
        return int(cur.lastrowid or 0)


def _save_works(account_id: int, platform: str, items: List[Dict[str, Any]]) -> int:
    """作品数据 upsert（同一 item_id 覆盖更新）。"""
    now = _now()
    saved = 0
    with write_conn() as conn:
        for it in items:
            item_id = str(_pick(it, "item_id", "gid_str", "group_id", "id") or "").strip()
            title = str(_pick(it, "title", "article_title", "name") or "").strip()
            if not item_id or not title:
                continue
            status = str(_pick(it, "status", "audit_status", "state", "check_status") or "")
            conn.execute(
                """INSERT INTO insight_works
                   (account_id, platform, item_id, title, status, show_count, read_count,
                    comment_count, like_count, publish_time, captured_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id, item_id) DO UPDATE SET
                     title=excluded.title, status=excluded.status,
                     show_count=excluded.show_count, read_count=excluded.read_count,
                     comment_count=excluded.comment_count, like_count=excluded.like_count,
                     publish_time=excluded.publish_time, captured_at=excluded.captured_at""",
                (account_id, platform, item_id, title, status,
                 _num(_pick(it, "show_count", "impression_count", "show")),
                 _num(_pick(it, "read_count", "click_count", "go_detail_count", "read")),
                 _num(_pick(it, "comment_count", "comments")),
                 _num(_pick(it, "like_count", "digg_count", "like")),
                 str(_pick(it, "publish_time", "create_time", "be_publish_time") or "")[:19],
                 now))
            saved += 1
    return saved


# ---------------------------------------------------------------- 采集入口
def collect_account(account_id: int,
                    kinds: Optional[List[str]] = None) -> Dict[str, Any]:
    """采集一个账号的数据（默认 作品列表 + 账号概览 + 粉丝分布）。"""
    acc = accounts.get_account(account_id, include_cookie=True)
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    platform = str(acc.get("platform") or "")
    pages = PAGES.get(platform) or {}
    if not pages:
        return {"ok": False, "error": f"{platform} 暂不支持数据采集（目前只做头条）"}
    want = [k for k in (kinds or ["works", "overview", "fans"]) if k in pages]
    result: Dict[str, Any] = {"account_id": account_id, "platform": platform,
                              "kinds": {}, "works_saved": 0}
    try:
        with _session(acc) as ctx:
            for kind in want:
                payloads = _collect_page(ctx, pages[kind])
                if kind == "overview":
                    # 账号级指标单独存一份：页面数据接口常年返回空（新号尤其明显），
                    # 这个端点至少能给出昵称 / 粉丝数，别让看板整页空白。
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    info = _fetch_json(page, _USER_API)
                    if isinstance(info, dict) and info:
                        _save_snapshot(account_id, platform, "account", [info])
                        result["account"] = _account_metrics(info)
                    if not payloads and isinstance(info, dict) and info:
                        payloads = [info]
                _save_snapshot(account_id, platform, kind, payloads)
                result["kinds"][kind] = len(payloads)
                if kind == "works":
                    items = _extract_items(payloads)
                    result["works_saved"] = _save_works(account_id, platform, items)
    except Exception as exc:  # noqa: BLE001 - 采集失败只影响看板
        result.update({"ok": False, "error": str(exc)[:300]})
        return result
    # 命中接口数为 0 就是没采到东西：不能因为 kinds 字典非空就判成功
    result["ok"] = any(int(v or 0) > 0 for v in (result.get("kinds") or {}).values())
    if not result["ok"]:
        result["error"] = result.get("error") or (
            "没抓到数据接口：确认账号已登录，且账号里有已发布作品（新号 / 只有草稿时没有数据）")
    return result


# ---------------------------------------------------------------- 查询
def overview(account_id: int = 0) -> Dict[str, Any]:
    where, args = "", []
    if int(account_id or 0):
        where, args = " WHERE account_id=?", [int(account_id)]
    with get_conn() as conn:
        works = [dict(r) for r in conn.execute(
            f"SELECT * FROM insight_works{where}"
            " ORDER BY read_count DESC, id DESC LIMIT 200", args).fetchall()]
        snaps = [dict(r) for r in conn.execute(
            f"SELECT id, account_id, platform, kind, captured_at, length(payload) AS bytes"
            f" FROM insight_snapshots{where} ORDER BY id DESC LIMIT 20", args).fetchall()]
        account: Dict[str, Any] = {}
        if int(account_id or 0):
            row = conn.execute(
                "SELECT payload FROM insight_snapshots WHERE account_id=? AND kind='account'"
                " ORDER BY id DESC LIMIT 1", (int(account_id),)).fetchone()
            if row:
                try:
                    arr = json.loads(row["payload"])
                    if arr and isinstance(arr[0], dict):
                        account = _account_metrics(arr[0])
                except (TypeError, ValueError):
                    account = {}
    return {
        "works": works,
        "snapshots": snaps,
        "account": account,
        "totals": {
            "works": len(works),
            "read": sum(int(w.get("read_count") or 0) for w in works),
            "show": sum(int(w.get("show_count") or 0) for w in works),
            "comment": sum(int(w.get("comment_count") or 0) for w in works),
        },
    }
