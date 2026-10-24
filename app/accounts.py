"""发布账号管理：按平台分类维护账号，用独立浏览器 profile 保存登录态。

设计要点：
- 每个账号绑定一个**独立的浏览器用户数据目录**（profile_dir）。登录态、密码、
  插件状态全存在这个目录里，程序只负责「打开它」，不存在库里，避免凭据泄露；
- cookie 是 profile 的副产物：需要发布时抓出来用，平时不碰；
- cookie 刷新分两种：
  * 手动：开**有头**浏览器让你自己登录，登完点「保存」，程序读 cookie 落库；
  * 自动：开**无头**浏览器访问后台首页，把还活着的会话续期，续不动就标记过期。
"""
import base64
import datetime as dt
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import settings
from .db import get_conn, write_conn

try:  # 浏览器能力是可选的，没装不影响其它功能
    from playwright.sync_api import sync_playwright  # type: ignore
    _HAS_PW = True
except ImportError:  # pragma: no cover
    sync_playwright = None  # type: ignore
    _HAS_PW = False

PW_HINT = "未安装 playwright：pip install playwright && playwright install chromium"

# 平台预设：key 落到 publish_accounts.platform
PLATFORMS: List[Dict[str, str]] = [
    {"key": "toutiao", "title": "今日头条", "home": "https://mp.toutiao.com/",
     "login": "https://mp.toutiao.com/auth/page/login/"},
    {"key": "baijia", "title": "百家号", "home": "https://baijiahao.baidu.com/",
     "login": "https://baijiahao.baidu.com/builder/rc/edit",
     # 百家号自己的登录页只是弹 passport 弹窗，页脚还摆着一张「用百度App打开创作中心」
     # 的宣传码（扫了不登录），所以扫码登录直接进 passport 扫码页，扫完回创作中心。
     "qr_login": "https://passport.baidu.com/v2/?login&tpl=bjh"
                 "&u=https%3A%2F%2Fbaijiahao.baidu.com%2Fbuilder%2Frc%2Fedit"},
    {"key": "zhihu", "title": "知乎", "home": "https://www.zhihu.com/creator",
     "login": "https://www.zhihu.com/signin"},
    {"key": "gongzhonghao", "title": "微信公众号", "home": "https://mp.weixin.qq.com/",
     "login": "https://mp.weixin.qq.com/"},
    {"key": "douyin", "title": "抖音", "home": "https://creator.douyin.com/",
     "login": "https://creator.douyin.com/"},
    {"key": "kuaishou", "title": "快手", "home": "https://cp.kuaishou.com/",
     "login": "https://cp.kuaishou.com/"},
    {"key": "xiaohongshu", "title": "小红书", "home": "https://creator.xiaohongshu.com/",
     "login": "https://creator.xiaohongshu.com/"},
    {"key": "bilibili", "title": "哔哩哔哩", "home": "https://member.bilibili.com/",
     "login": "https://passport.bilibili.com/login"},
    {"key": "weibo", "title": "微博", "home": "https://weibo.com/",
     "login": "https://passport.weibo.com/sso/signin"},
    {"key": "custom", "title": "自定义平台", "home": "", "login": ""},
]

EDITABLE = {
    "platform", "name", "login_user", "home_url", "profile_dir", "executable_path",
    "headless", "proxy", "auto_refresh", "refresh_hours", "enabled", "note",
}


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def meta() -> Dict[str, Any]:
    return {"platforms": PLATFORMS, "browser_ready": _HAS_PW,
            "hint": "" if _HAS_PW else PW_HINT,
            "profile_root": str(profile_root())}


# ---------------------------------------------------------------- CRUD
def _mask(d: Dict[str, Any]) -> Dict[str, Any]:
    """列表里不回传完整 cookie，只给条数与过期时间。"""
    raw = str(d.get("cookie") or "")
    n = 0
    if raw:
        try:
            n = len(json.loads(raw))
        except ValueError:
            n = raw.count(";") + 1 if raw.strip() else 0
    d["cookie_count"] = n
    d.pop("cookie", None)
    return d


def list_accounts(platform: str = "", include_cookie: bool = False) -> List[Dict[str, Any]]:
    where = " WHERE platform=?" if platform else ""
    args = [platform] if platform else []
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM publish_accounts{where} ORDER BY platform, id", args).fetchall()
    items = [dict(r) for r in rows]
    for d in items:
        d["platform_title"] = platform_title(d.get("platform"))
        d["cookie_state"] = cookie_state(d)
        if not include_cookie:
            _mask(d)
    return items


def platform_title(key: Optional[str]) -> str:
    for p in PLATFORMS:
        if p["key"] == key:
            return p["title"]
    return key or "自定义"


def get_account(account_id: int, include_cookie: bool = False) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM publish_accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["platform_title"] = platform_title(d.get("platform"))
    d["cookie_state"] = cookie_state(d)
    if not include_cookie:
        _mask(d)
    return d


def save_endpoints(account_id: int, buckets: Dict[str, Any]) -> None:
    """把探测抓到的真实接口存下来，分发时直接复用，不用每次重新抓。"""
    with write_conn() as conn:
        conn.execute(
            "UPDATE publish_accounts SET api_endpoints=?, updated_at=? WHERE id=?",
            (json.dumps(buckets, ensure_ascii=False), _now(), account_id))


def upsert(data: Dict[str, Any], account_id: Optional[int] = None) -> int:
    now = _now()
    payload = {k: v for k, v in (data or {}).items() if k in EDITABLE}
    if not account_id and not str(payload.get("name") or "").strip():
        raise ValueError("请填写账号名称")
    raw_profile = str(payload.get("profile_dir") or "").strip()
    if not raw_profile and account_id:
        # 编辑时没填就沿用原来的，别把已经存好的登录态挪走
        with get_conn() as c:
            row = c.execute("SELECT profile_dir FROM publish_accounts WHERE id=?",
                            (account_id,)).fetchone()
        raw_profile = str(row["profile_dir"] or "").strip() if row else ""
    # 留空、或填的是 Windows 盘符 / /mnt 挂载路径 —— 都换成本机目录，
    # 否则 chromium 迟早卡在单例锁上（NTFS 挂载盘上的 profile 各种不可靠）
    if not raw_profile or looks_unusable(raw_profile):
        raw_profile = suggest_profile_dir(
            str(payload.get("platform") or ""), str(payload.get("name") or ""), account_id)
    payload["profile_dir"] = raw_profile
    with write_conn() as conn:
        if account_id:
            row = conn.execute("SELECT id FROM publish_accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise LookupError("账号不存在")
            if payload:
                sets = ", ".join(f"{k}=?" for k in payload) + ", updated_at=?"
                conn.execute(f"UPDATE publish_accounts SET {sets} WHERE id=?",
                             [*payload.values(), now, account_id])
        else:
            preset = next((p for p in PLATFORMS if p["key"] == payload.get("platform")), None)
            payload.setdefault("home_url", (preset or {}).get("home", ""))
            cols = ", ".join(payload) + ", created_at, updated_at"
            marks = ", ".join("?" * (len(payload) + 2))
            cur = conn.execute(f"INSERT INTO publish_accounts ({cols}) VALUES ({marks})",
                               [*payload.values(), now, now])
            account_id = int(cur.lastrowid or 0)
    return int(account_id)


def _purge_profile(raw: Any) -> tuple:
    """删掉账号的浏览器目录，返回 (释放字节数, 目录路径)。

    只允许删 profile_root 底下的目录 —— profile_dir 是库里的字符串，
    万一被改成 ../.. 或 /etc 就是灾难，必须做根目录校验。
    """
    import shutil
    from .publish.base import normalize_path
    s = str(raw or "").strip()
    if not s:
        return 0, ""
    try:
        target = Path(normalize_path(s)).resolve()
        root = profile_root().resolve()
    except Exception:  # noqa: BLE001
        return 0, ""
    if target == root or root not in target.parents:
        return 0, ""
    size = 0
    for p in target.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                size += p.stat().st_size
        except OSError:
            continue
    # 关掉浏览器后 chromium 可能还在把缓存写回目录，等它彻底退出再删，
    # 否则刚删完目录又被重建（实践中见过留下几十 MB 的空壳 profile）。
    time.sleep(0.6)
    shutil.rmtree(target, ignore_errors=True)
    return size, str(target)


def delete(account_id: int, purge_profile: bool = True,
           purge_tasks: bool = False) -> Dict[str, Any]:
    """删除账号；默认连浏览器 profile 一起清（登录态 + 缓存全在里面）。"""
    acc = get_account(account_id)
    if not acc:
        return {"deleted": 0}
    # 正在登录 / 刷新的会话先停掉，否则 chromium 会把删到一半的目录又写回去
    holder = _REFRESH_HOLDER.get(account_id)
    if holder:
        _shutdown(holder)
        with _LOCK:
            _REFRESH_HOLDER.pop(account_id, None)
    with write_conn() as conn:
        conn.execute("DELETE FROM publish_accounts WHERE id=?", (account_id,))
        if purge_tasks:
            conn.execute("DELETE FROM publish_tasks WHERE account_id=?", (account_id,))
    _set_state(account_id, running=False, phase="cancelled", qr_path="",
               message="账号已删除")
    freed, removed = _purge_profile(acc.get("profile_dir")) if purge_profile else (0, "")
    return {"deleted": account_id, "profile_removed": removed, "freed_bytes": freed}


def set_enabled(account_id: int, enabled: bool) -> Optional[Dict[str, Any]]:
    with write_conn() as conn:
        cur = conn.execute("UPDATE publish_accounts SET enabled=?, updated_at=? WHERE id=?",
                           (1 if enabled else 0, _now(), account_id))
    return get_account(account_id) if cur.rowcount else None


# ---------------------------------------------------------------- cookie 状态
def cookie_expire_at(cookies: List[Dict[str, Any]]) -> Optional[dt.datetime]:
    """取一堆 cookie 里最近的过期时间。

    会话 cookie（expires <= 0）没有明确的过期时间点，不能算作「已过期」——
    粘过来的 cookie 基本都是这种（请求头里不带过期时间），判成过期会让人以为导入失败了。
    过期时间未知时返回 None，交给「测登录」去验证。
    """
    stamps: List[float] = []
    for c in cookies or []:
        exp = c.get("expires") or c.get("expiry") or c.get("expirationDate")
        if exp is None:
            continue
        try:
            exp = float(exp)
        except (TypeError, ValueError):
            continue
        if exp <= 0:      # 会话 cookie：跳过，不参与过期判定
            continue
        stamps.append(exp)
    if not stamps:
        return None
    return dt.datetime.fromtimestamp(min(stamps))


def cookie_state(d: Dict[str, Any]) -> str:
    if d.get("status") == "error":
        return "error"
    expire = str(d.get("cookie_expire_at") or "")
    if not str(d.get("cookie") or "").strip():
        return "empty"
    if not expire:
        return "unknown"
    try:
        when = dt.datetime.strptime(expire, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "unknown"
    if when <= dt.datetime.now():
        return "expired"
    if when - dt.datetime.now() < dt.timedelta(hours=24):
        return "expiring"
    return "valid"


STATE_TEXT = {
    "empty": "未抓取", "unknown": "状态未知", "valid": "有效",
    "expiring": "即将过期", "expired": "已过期", "error": "刷新失败",
}

# ---------------------------------------------------------------- 浏览器刷新
_REFRESH: Dict[int, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def refresh_state(account_id: int) -> Dict[str, Any]:
    with _LOCK:
        return dict(_REFRESH.get(account_id) or {"running": False, "phase": "", "message": ""})


def _set_state(account_id: int, **kw: Any) -> None:
    with _LOCK:
        cur = _REFRESH.get(account_id, {})
        cur.update(kw)
        _REFRESH[account_id] = cur


# 降低「一眼就是自动化」的特征：这几项对齐 toutiao-ops-main 的 launch 参数，
# 扫码登录后立刻发请求不太容易被风控拦（有些平台登录成功也会因为指纹异常被判环境异常）。
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-dev-shm-usage",
]


def _browser_kwargs(acc: Dict[str, Any], force_headed: bool = False) -> Dict[str, Any]:
    """浏览器启动参数。

    force_headed：手动登录必须让人看得见窗口并操作，此时忽略账号的 headless 设置。
    """
    exe = str(acc.get("executable_path") or settings.get_setting("browser.executable_path") or "").strip()
    proxy = str(acc.get("proxy") or settings.get_setting("fetch.proxy") or "").strip()
    kwargs: Dict[str, Any] = {
        "headless": False if force_headed else bool(acc.get("headless")),
        "args": list(_STEALTH_ARGS),
        # 去掉 playwright 自带的 --enable-automation，否则 navigator.webdriver 一直是 true
        "ignore_default_args": ["--enable-automation"],
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "viewport": {"width": 1440, "height": 900},
    }
    if exe:
        # WSL/本机 Linux 跑不了 Windows 的 chrome.exe，忽略它退回内置 chromium
        if re.match(r"^[A-Za-z]:", exe) and os.name != "nt":
            pass
        else:
            kwargs["executable_path"] = exe
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    return kwargs


_SAFE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


def profile_root() -> Path:
    """chromium 用户数据目录的根，必须落在 WSL 本地文件系统上。

    /mnt/c /mnt/e 这些是 NTFS 挂载盘，chromium 把 user-data-dir 放那儿会出
    「单例锁状态不一致」「mmap/WAL 不可靠」一类问题，所以统一挪到本机路径。
    """
    root = Path(os.environ.get("AUTONEWS_PROFILE_ROOT") or "/var/lib/autonews/profiles")
    try:
        root.mkdir(parents=True, exist_ok=True)
        return root
    except OSError:
        fallback = Path("/tmp/autonews-profiles")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def suggest_profile_dir(platform: str, name: str,
                        account_id: Optional[int] = None) -> str:
    """给账号生成一个 WSL 本地的、互不冲突的 profile 目录。"""
    def safe(s: str) -> str:
        return str(_SAFE_RE.sub("-", s or "").strip("-"))

    head = safe(platform) or "acc"
    tail = safe(name)
    stem = f"{head}-{tail}" if tail else head
    root = profile_root()
    target = root / stem
    # 别的账号已经占了就加 id 区分，避免两个账号共用一个 profile 互相顶登录态
    if _dir_used_by_other(target, account_id):
        target = root / f"{stem}-{account_id}"
    return str(target)


def _dir_used_by_other(target: Path, account_id: Optional[int]) -> bool:
    if account_id is None:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM publish_accounts WHERE (profile_dir=? OR profile_dir=?)"
            " AND id<>?", (str(target), str(target) + "/", account_id)).fetchone()
    return bool(row["c"])


def looks_unusable(raw: str) -> bool:
    """Windows 盘符路径，或 /mnt/ 下的挂载路径 —— 都不适合放 chromium 用户数据。"""
    from .publish.base import normalize_path
    s = str(raw or "").strip()
    if not s:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", s):
        return True
    return normalize_path(s).startswith("/mnt/")


def _profile_dir(acc: Dict[str, Any]) -> Optional[Path]:
    raw = str(acc.get("profile_dir") or "").strip()
    if not raw:
        return None
    # 用户可能填的是 Windows 盘符路径，WSL 里要转成 /mnt/e/...
    from .publish.base import normalize_path
    p = Path(normalize_path(raw))
    p.mkdir(parents=True, exist_ok=True)
    return p


def parse_cookies(raw: str, domain: str = "") -> List[Dict[str, Any]]:
    """把各种格式的 cookie 文本统一转成 playwright 的 cookie 字典列表。

    支持三种写法：
    * JSON 数组 —— EditThisCookie / Cookie-Editor 插件导出的 `[{name, value, domain}]`
    * Netscape 文件 —— 每行制表符分隔
    * `a=1; b=2` —— 从浏览器 F12 → Network → Request Headers 里复制的 Cookie 请求头
    """
    text = str(raw or "").strip()
    if not text:
        return []

    def _norm(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        name = str(item.get("name") or "").strip()
        if not name:
            return None
        exp_raw: Any = -1.0
        for key in ("expires", "expiry", "expirationDate"):
            cand = item.get(key)
            if cand is not None:
                exp_raw = cand
                break
        try:
            exp = float(exp_raw)
        except (TypeError, ValueError):
            exp = -1.0
        return {
            "name": name, "value": str(item.get("value") or ""),
            # domain 保留成 ".toutiao.com" 这种带点写法：注入浏览器时要靠它匹配子域
            "domain": str(item.get("domain") or domain or ""),
            "path": str(item.get("path") or "/"), "expires": exp,
            "httpOnly": bool(item.get("httpOnly")), "secure": bool(item.get("secure")),
            "sameSite": str(item.get("sameSite") or "Lax"),
        }

    # 1. JSON（纯数组，或 {"cookies": [...]}）
    if text[0] in "[{":
        try:
            data = json.loads(text)
            items = data if isinstance(data, list) else (data.get("cookies") or [])
            out = [x for x in (_norm(i) for i in items) if x]
            if out:
                return out
        except (ValueError, AttributeError, TypeError):
            pass

    # 2. Netscape 格式
    first = text.splitlines()[0] if text.splitlines() else ""
    if " netscape" in first.lower() or "\t" in first:
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 7:
                continue
            dom, _flag, path, secure, exp, name, value = cols[:7]
            x = _norm({"domain": dom, "path": path, "name": name, "value": value,
                       "secure": str(secure).upper() == "TRUE", "expires": exp})
            if x:
                out.append(x)
        if out:
            return out

    # 3. 请求头串 —— 没有 domain 信息，用账号后台域名兜底
    out = []
    for part in re.split(r";\s*", text):
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        x = _norm({"name": name.strip(), "value": value.strip(), "domain": domain})
        if x:
            out.append(x)
    return out


def save_cookie_text(account_id: int, raw: str) -> Dict[str, Any]:
    """手动粘贴 cookie。不打不开浏览器也能用（WSLg 不可用时的兜底入口）。"""
    acc = get_account(account_id)
    if not acc:
        raise LookupError("账号不存在")
    domain = ""
    home = str(acc.get("home_url") or "").strip()
    if home:
        try:
            from urllib.parse import urlparse
            host = urlparse(home).hostname or ""
            if host:
                domain = "." + host.lstrip(".")
        except Exception:  # noqa: BLE001
            domain = ""
    cookies = parse_cookies(raw, domain)
    if not cookies:
        raise ValueError("没解析出任何 cookie。检查格式："
                         "`name=value; name2=value2`，或插件导出的 JSON 数组")
    return _save_cookies(account_id, cookies)


def _save_cookies(account_id: int, cookies: List[Dict[str, Any]]) -> Dict[str, Any]:
    expire = cookie_expire_at(cookies)
    now = _now()
    with write_conn() as conn:
        conn.execute(
            "UPDATE publish_accounts SET cookie=?, cookie_updated_at=?, cookie_expire_at=?,"
            " status=?, last_checked_at=?, last_error='', updated_at=? WHERE id=?",
            (json.dumps(cookies, ensure_ascii=False), now,
             expire.strftime("%Y-%m-%d %H:%M:%S") if expire else "",
             # 不确定过期时间的算 unknown（待验证），别直接标有效，也别误判成过期
             ("expired" if expire and expire < dt.datetime.now()
              else "valid" if expire else "unknown") if cookies else "unknown",
             now, now, account_id))
    return get_account(account_id) or {}


_REFRESH_HOLDER: Dict[int, Dict[str, Any]] = {}


def _fetch_cookies(context: Any) -> List[Dict[str, Any]]:
    """读 cookie。context 已经关掉时会抛，转成一句话能听懂的错误。"""
    try:
        return list(context.cookies())
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "浏览器窗口已经不在了，读不到 cookie。重新点「刷新 Cookie」打开新窗口，"
            "在里面登录完再保存（别中途关窗口）") from exc


_LOGIN_WORDS = ("login", "passport", "signin", "auth/page")


def _browser_alive(ctx: Any) -> bool:
    """浏览器进程还在不在。

    实测（强杀 chromium 后）：
      - `close` 事件：不触发
      - `ctx.pages`：照常返回列表
      - `browser.is_connected()`：仍返回 True（状态是缓存的，不可信）
      - `ctx.cookies()`：立刻抛 TargetClosedError ✅
    所以必须发一个真正走 CDP 的调用才算数。
    """
    try:
        b = getattr(ctx, "browser", None)
        if b is not None and hasattr(b, "is_connected") and not b.is_connected():
            return False                  # 明确断开时省一次通信
        ctx.cookies()                     # 真正的探活：进程没了必抛错
        return True
    except Exception:  # noqa: BLE001
        return False


def _safe_cookies(ctx: Any) -> List[Dict[str, Any]]:
    """读 cookie，窗口没了就当空，不抛。"""
    if not ctx:
        return []
    try:
        return list(ctx.cookies())
    except Exception:  # noqa: BLE001
        return []


def _current_url(ctx: Any) -> str:
    try:
        for p in ctx.pages:
            u = str(p.url or "")
            if u.startswith("http"):
                return u
    except Exception:  # noqa: BLE001
        pass
    return ""


def _guard_refresh_session(holder: Dict[str, Any], account_id: int,
                           start_url: str) -> None:
    """盯着手动刷新的会话，干两件事：

    1. 登录一完成就把 cookie 自动落库 —— 登录完就算直接关窗口也不丢；
    2. 发现窗口没了立刻把状态改成失败 —— 否则页面还挂着「保存 Cookie」，
       用户点了只会看到「读到 0 条 cookie」，不知道浏览器早不存在了。
    """
    was_login = any(w in str(start_url or "").lower() for w in _LOGIN_WORDS)
    base = len(_safe_cookies(holder.get("context")))
    deadline = time.time() + 30 * 60        # 最多盯半小时
    while time.time() < deadline:
        time.sleep(2)
        if not refresh_state(account_id).get("running"):
            return                          # 已经手动保存 / 取消 / 失败
        ctx = holder.get("context")
        if not ctx:
            return
        if not _browser_alive(ctx):
            with _LOCK:
                _REFRESH_HOLDER.pop(account_id, None)
            _set_state(account_id, running=False, phase="failed",
                       message="浏览器窗口被关闭了，cookie 还没存进来。"
                               "再点一次「刷新 Cookie」——登录态留在 profile 里，"
                               "新窗口打开通常直接就是登录状态")
            return
        cookies = _safe_cookies(ctx)
        if not was_login or len(cookies) <= base:
            continue
        cur = _current_url(ctx)
        if cur and not any(w in cur.lower() for w in _LOGIN_WORDS):
            _save_cookies(account_id, cookies)
            _set_state(account_id, running=False, phase="done",
                       message=f"检测到登录完成，已自动保存 {len(cookies)} 条 cookie，"
                               f"窗口可以直接关掉了")
            return


def _ensure_profile_dir(account_id: int, acc: Dict[str, Any]) -> Optional[Path]:
    """给没配目录的老账号补一个本机 profile 路径并落库。"""
    suggested = suggest_profile_dir(str(acc.get("platform") or ""),
                                    str(acc.get("name") or ""), account_id)
    try:
        with write_conn() as conn:
            conn.execute(
                "UPDATE publish_accounts SET profile_dir=?, updated_at=? WHERE id=?",
                (suggested, _now(), account_id))
    except Exception:  # noqa: BLE001 - 写库失败也要能继续，用内存值先把浏览器打开
        pass
    return _profile_dir({"profile_dir": suggested})


def start_refresh(account_id: int, mode: str = "manual") -> Dict[str, Any]:
    """开一个浏览器让你登录（manual）或后台续期（auto）。"""
    acc = get_account(account_id)
    if not acc:
        raise LookupError("账号不存在")
    if not _HAS_PW:
        raise RuntimeError(PW_HINT)
    if refresh_state(account_id).get("running"):
        return refresh_state(account_id)
    profile = _profile_dir(acc)
    if not profile:
        # 历史账号（或早期留空建的）没有目录，补一个本机路径落库，别把人卡在这一步
        profile = _ensure_profile_dir(account_id, acc)
    if not profile:
        raise ValueError("浏览器用户数据目录不可用，请检查磁盘权限")

    # 同一个 profile 目录不能被两个 chromium 同时占着，先收拾上一次的残留
    old_holder = _REFRESH_HOLDER.get(account_id)
    if old_holder:
        _shutdown(old_holder)
    holder: Dict[str, Any] = {"context": None, "pw": None}
    with _LOCK:
        _REFRESH_HOLDER[account_id] = holder
    _set_state(account_id, running=True, phase="launching",
               message="正在启动浏览器…", mode=mode, started_at=_now())

    def launch(pw_obj: Any, force_headed: bool) -> Any:
        from .publish.base import clear_profile_locks, has_running_browser, in_use_error
        kwargs = _browser_kwargs(acc, force_headed=force_headed)
        try:
            return pw_obj.chromium.launch_persistent_context(str(profile), **kwargs)
        except Exception as exc:  # noqa: BLE001
            # 上次直接关窗口会留 SingletonLock，这里清掉再试一次
            if in_use_error(exc) and not has_running_browser(profile):
                clear_profile_locks(profile)
                return pw_obj.chromium.launch_persistent_context(str(profile), **kwargs)
            raise

    def worker() -> None:
        try:
            pw = sync_playwright().start()
            holder["pw"] = pw
            browser = launch(pw, force_headed=(mode == "manual"))
            holder["context"] = browser
            page = browser.pages[0] if browser.pages else browser.new_page()
            url = str(acc.get("home_url") or "").strip() or \
                next((p["login"] or p["home"] for p in PLATFORMS if p["key"] == acc.get("platform")), "")
            if url:
                try:
                    page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                except Exception:  # noqa: BLE001 - 首屏超时不影响手动登录
                    pass
            if mode == "auto":
                # 无头续期：给页面一点时间完成跳转与种 cookie，然后直接收
                _set_state(account_id, phase="waiting", message="正在读取登录态…")
                time.sleep(6)
                try:
                    cookies = _fetch_cookies(browser)
                except RuntimeError:      # 浏览器中途没了，等同读不到
                    cookies = []
                if cookies:
                    _save_cookies(account_id, cookies)
                    _set_state(account_id, running=False, phase="done",
                               message=f"已续期，抓到 {len(cookies)} 条 cookie")
                else:
                    with write_conn() as conn:
                        conn.execute(
                            "UPDATE publish_accounts SET status='expired', last_checked_at=?,"
                            " last_error=? WHERE id=?",
                            (_now(), "无头续期读不到 cookie，登录态可能已失效，请手动刷新", account_id))
                    _set_state(account_id, running=False, phase="failed",
                               message="读不到 cookie，登录态可能已失效，请手动刷新")
                _shutdown(holder)
                return
            _set_state(account_id, phase="waiting",
                       message="浏览器已打开，请在里面完成登录；登录成功会自动保存，"
                               "也可以手动点「我已登录，保存 Cookie」")
            current = ""
            try:
                current = page.url or ""
            except Exception:  # noqa: BLE001
                pass
            _guard_refresh_session(holder, account_id, current)
        except Exception as exc:  # noqa: BLE001
            with write_conn() as conn:
                conn.execute(
                    "UPDATE publish_accounts SET status='error', last_error=?, last_checked_at=? WHERE id=?",
                    (str(exc)[:300], _now(), account_id))
            _set_state(account_id, running=False, phase="failed", message=str(exc)[:300])
            _shutdown(holder)

    threading.Thread(target=worker, daemon=True, name=f"acct-refresh-{account_id}").start()
    return refresh_state(account_id)


def _shutdown(holder: Dict[str, Any]) -> None:
    for key in ("context", "pw"):
        obj = holder.get(key)
        if not obj:
            continue
        try:
            obj.close() if key == "context" else obj.stop()
        except Exception:  # noqa: BLE001
            pass
        holder[key] = None


# ---------------------------------------------------------------- 扫码登录
# 取码优先级：先按 <img> 的 src 直接下原图，再按元素截图，最后整页截图兜底。
# 为什么不是一上来就截元素：头条的码是 data: base64 的 <img>；百家号的登录码藏在
# passport 弹窗里（元素 0x0，截不到，但 src 已经生成），按 src 下才拿得到。
_QR_IMG_SELECTORS = (
    # 百度 passport 登录码（百家号等百度系）
    'img.tang-pass-qrcode-img',
    'img[src*="passport.baidu.com/v2/api/qrcode"]',
    # 头条系
    'img.web-login-scan-code__content__qrcode-wrapper__qrcode',
    '[class*="scan-code"] img',
    # 通用：二维码容器里的第一张图
    '[class*="qrcode"] img', '[class*="qr-code"] img', '[class*="qr_code"] img',
    '[class*="QRCode"] img', '[id*="qrcode"] img', '[id*="qr"] img',
    'img[src*="qrcode"]', 'img[src*="qr"]',
)
# 元素截图的选择器（「先精确后宽松」）；头条/百家登录页改版频繁，全落空时退回整页截图
_QR_SELECTORS = (
    '[class*="qrcode"]', '[class*="qr-code"]', '[class*="qr_code"]',
    '[class*="QRCode"]', '[class*="scan"]', '[class*="web-login"]',
    'img[src*="qr"]', 'canvas',
)
# 页脚 / 下载 App / 活动推广位上的「宣传码」，扫了也不登录，必须排掉
_QR_PROMO_HINTS = ("footer", "download", "app-qr", "appqr", "promo",
                   "advert", "banner", "activity", "share")
# 这类内容开头的码是「拉起 App」或推广，不是登录码
_QR_NOT_LOGIN_HINTS = (
    "baiduboxapp://", "weixin://", "alipays://", "alipay://", "douyin://",
    "kwai://", "kuaishou://", "bilibili://", "xiaohongshu://", "xhsdiscover://",
    "zhihu://", "sinaweibo://", "weibo://", "baidu://",
)
_QR_REFRESH_SECONDS = 45      # 二维码一般 50-60 秒过期，提前重截
_QR_MAX_MINUTES = 6           # 最长等待扫码时长

# 登录成功后平台一定会种上会话类 cookie。用它判断「真的登录了」，
# 而不是只看 cookie 条数变多（很多站点一进首页就种一堆埋点 cookie）。
_LOGIN_COOKIE_HINTS = ("sessionid", "session", "passport", "token", "bduss",
                       "stoken", "sso", "ticket", "sid", "auth")


def _qr_file(account_id: int) -> Path:
    from .config import QR_DIR
    QR_DIR.mkdir(parents=True, exist_ok=True)
    return QR_DIR / f"account-{account_id}.png"


def login_qr_path(account_id: int) -> str:
    """当前账号最新二维码图片路径（没生成过 / 已被清掉时返回空串）。"""
    raw = str(refresh_state(account_id).get("qr_path") or "")
    return raw if raw and Path(raw).exists() else ""


def _login_url(acc: Dict[str, Any]) -> str:
    """扫码入口：平台配了专用扫码页就用它，否则用账号首页 / 平台预设登录页。"""
    preset = next((p for p in PLATFORMS if p["key"] == acc.get("platform")), None) or {}
    qr_login = str(preset.get("qr_login") or "").strip()
    if qr_login:
        return qr_login
    home = str(acc.get("home_url") or "").strip()
    if home:
        return home
    return str(preset.get("login") or preset.get("home") or "")


def _qr_payload(data: bytes) -> str:
    """尽力解码二维码内容。没装解码库就返回空串——只用来排除，不做硬校验。"""
    try:
        import io as _io
        import zxingcpp  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:  # noqa: BLE001
        return ""
    try:
        res = zxingcpp.read_barcodes(Image.open(_io.BytesIO(data)))
    except Exception:  # noqa: BLE001
        return ""
    return str(getattr(res[0], "text", "") or "") if res else ""


def _is_login_qr(data: bytes) -> bool:
    """解不出来就当能用（不做硬依赖）；解出来是推广 / 拉起 App 的码才否掉。"""
    text = _qr_payload(data).strip().lower()
    if not text:
        return True
    return not any(h in text for h in _QR_NOT_LOGIN_HINTS)


def _fetch_qr_src(page: Any, src: str) -> bytes:
    """按 <img> 的 src 取二维码原图：支持 data: base64 和 http(s) 直链。"""
    src = (src or "").strip()
    if not src:
        return b""
    if src.startswith("data:"):
        head, _, b64 = src.partition(",")
        if "base64" not in head:
            return b""
        try:
            return base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return b""
    if not src.startswith(("http://", "https://")):
        return b""
    clients = []
    for obj in (page, getattr(page, "context", None)):
        client = getattr(obj, "request", None)
        if client is not None:
            clients.append(client)
    for client in clients:
        try:
            resp = client.get(src, timeout=20_000)
            if resp.ok:
                return resp.body()
        except Exception:  # noqa: BLE001
            continue
    return b""


def _refresh_qr_if_expired(page: Any) -> None:
    """二维码过期后平台会在上面盖一层「点击刷新」，先点掉再取码。"""
    for sel in ('[class*="qrcode-status-refresh"]', '[class*="qr-refresh"]',
                '[class*="qrcode"] [class*="refresh"]'):
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=2_000)
                time.sleep(1.0)
                return
        except Exception:  # noqa: BLE001
            continue


def _capture_qr(page: Any, target: Path) -> bool:
    """把登录页上的**登录**二维码取出来（留下载原图 → 元素截图 → 整页截图）。"""
    _refresh_qr_if_expired(page)

    # 1) 按 src 下原图：无边框、无遮挡，隐藏元素也能取
    for sel in _QR_IMG_SELECTORS:
        try:
            for el in page.query_selector_all(sel)[:4]:
                cls = (el.get_attribute("class") or "").lower()
                if any(h in cls for h in _QR_PROMO_HINTS):
                    continue
                data = _fetch_qr_src(page, el.get_attribute("src") or "")
                if not data or not _is_login_qr(data):
                    continue
                target.write_bytes(data)
                return True
        except Exception:  # noqa: BLE001 - 单个选择器失败继续试下一个
            continue

    # 2) 元素截图：要求可见、够大、不是推广位
    for sel in _QR_SELECTORS:
        try:
            for el in page.query_selector_all(sel)[:4]:
                cls = (el.get_attribute("class") or "").lower()
                if any(h in cls for h in _QR_PROMO_HINTS) or not el.is_visible():
                    continue
                box = el.bounding_box()
                if not box:
                    continue
                w = float(box.get("width") or 0)
                h = float(box.get("height") or 0)
                if w < 80 or h < 80 or w * h > 900 * 900:
                    continue
                el.screenshot(path=str(target))
                if _is_login_qr(target.read_bytes()):
                    return True
        except Exception:  # noqa: BLE001
            continue

    try:
        page.screenshot(path=str(target))
        return True
    except Exception:  # noqa: BLE001
        return False


def _cookie_keys(cookies: List[Dict[str, Any]]) -> set:
    return {f"{c.get('name')}@{c.get('domain')}" for c in cookies or []}


def _fresh_login_cookies(cookies: List[Dict[str, Any]], before: set) -> List[Dict[str, Any]]:
    """本次新种上来的、看起来是会话凭证的 cookie。"""
    out = []
    for c in cookies or []:
        if f"{c.get('name')}@{c.get('domain')}" in before:
            continue
        name = str(c.get("name") or "").lower()
        if name and any(h in name for h in _LOGIN_COOKIE_HINTS):
            out.append(c)
    return out


# 手动刷新的「保存」动作：把当前浏览器的 cookie 收进库
def save_refresh(account_id: int) -> Dict[str, Any]:
    holder = _REFRESH_HOLDER.get(account_id)
    if not holder:
        raise RuntimeError("没有正在进行的刷新会话。先点「刷新 Cookie」，"
                           "等提示变成「请在里面完成登录」再保存")
    ctx = holder.get("context")
    if not ctx:
        raise RuntimeError("浏览器还没准备好（可能启动失败）。"
                           "看上面那条错误提示；若显示 composer/opengl 之类报错，多半是缺系统依赖")
    cookies = _fetch_cookies(ctx)
    if not cookies:
        raise RuntimeError("一个 cookie 都没读到：必须在刚弹出的那个浏览器窗口里登录才算，"
                           "在本机 Chrome / 别的浏览器里登录不算（两边 profile 不通用）")
    item = _save_cookies(account_id, cookies)
    _set_state(account_id, running=False, phase="done",
               message=f"已保存 {len(cookies)} 条 cookie")
    _shutdown(holder)
    return item


def cancel_refresh(account_id: int) -> Dict[str, Any]:
    holder = _REFRESH_HOLDER.get(account_id) or {}
    _shutdown(holder)
    _set_state(account_id, running=False, phase="cancelled", message="已取消")
    return refresh_state(account_id)


def start_login(account_id: int) -> Dict[str, Any]:
    """扫码登录：无头打开登录页，把二维码截给前端，扫码后自动落库。

    这是「不再手输 cookie」的主路径：
    1. 二维码存 data/qrcode/account-<id>.png，前端轮询状态拿 qr_path 展示；
    2. 每 45 秒重截（二维码会过期），最多等 6 分钟；
    3. 检测到新种了会话类 cookie 且已离开登录页 → 判定成功并落库；
    4. 关掉浏览器（释放 profile）后再跑一次平台检测，写回昵称/UID。
    """
    acc = get_account(account_id)
    if not acc:
        raise LookupError("账号不存在")
    if not _HAS_PW:
        raise RuntimeError(PW_HINT)
    if refresh_state(account_id).get("running"):
        return refresh_state(account_id)
    profile = _profile_dir(acc) or _ensure_profile_dir(account_id, acc)
    if not profile:
        raise ValueError("浏览器用户数据目录不可用，请检查磁盘权限")

    old = _REFRESH_HOLDER.get(account_id)
    if old:
        _shutdown(old)
    holder: Dict[str, Any] = {"context": None, "pw": None}
    with _LOCK:
        _REFRESH_HOLDER[account_id] = holder
    _set_state(account_id, running=True, phase="launching", started_at=_now(),
               mode="qrcode", qr_path="", message="正在打开登录页…")

    def worker() -> None:
        try:
            from .publish.base import clear_profile_locks, has_running_browser, in_use_error
            pw = sync_playwright().start()
            holder["pw"] = pw
            kwargs = _browser_kwargs(acc)
            kwargs["headless"] = True          # 扫码不需要看见窗口
            try:
                ctx = pw.chromium.launch_persistent_context(str(profile), **kwargs)
            except Exception as exc:  # noqa: BLE001
                if in_use_error(exc) and not has_running_browser(profile):
                    clear_profile_locks(profile)
                    ctx = pw.chromium.launch_persistent_context(str(profile), **kwargs)
                else:
                    raise
            holder["context"] = ctx
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            url = _login_url(acc)
            if url:
                try:
                    page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                except Exception:  # noqa: BLE001 - 首屏超时不影响后续截图
                    pass
            time.sleep(4)                      # 等登录框 / 二维码渲染出来
            if not _guard_login_session(holder, account_id, page):
                return
            # 先放掉 profile 再校验：同一目录不能被两个 chromium 同时占着
            _shutdown(holder)
            holder["context"] = None
            holder["pw"] = None
            time.sleep(1)
            uname = _verify_after_login(account_id)
            _set_state(account_id, running=False, phase="done", qr_path="",
                       message=f"登录成功{'（' + uname + '）' if uname else ''}，已保存登录态")
        except Exception as exc:  # noqa: BLE001
            with write_conn() as conn:
                conn.execute(
                    "UPDATE publish_accounts SET last_error=?, last_checked_at=? WHERE id=?",
                    (f"扫码登录失败：{str(exc)[:250]}", _now(), account_id))
            _set_state(account_id, running=False, phase="failed",
                       message=str(exc)[:300], qr_path="")
        finally:
            with _LOCK:
                _REFRESH_HOLDER.pop(account_id, None)
            _shutdown(holder)

    threading.Thread(target=worker, daemon=True, name=f"acct-login-{account_id}").start()
    return refresh_state(account_id)


def _verify_after_login(account_id: int) -> str:
    """登录成功后跑一次平台检测，把昵称 / UID 写回账号。

    失败不改 status（cookie 已经落库了，别因为一次检测不稳就标成错误），
    只把原因记进 last_error，用户点「测登录」还能再验一次。
    """
    acc = get_account(account_id, include_cookie=True)
    if not acc:
        return ""
    try:
        from . import publish as publish_mod
        driver = publish_mod.get_driver(acc)
        res = driver.check_auth() if driver else None
    except Exception as exc:  # noqa: BLE001
        res = None
        with write_conn() as conn:
            conn.execute("UPDATE publish_accounts SET last_error=?, last_checked_at=? WHERE id=?",
                         (f"登录检测失败：{str(exc)[:200]}", _now(), account_id))
    if not res:
        return ""
    with write_conn() as conn:
        if res.ok:
            conn.execute(
                "UPDATE publish_accounts SET login_user=?, status='valid', last_error='',"
                " last_checked_at=?, updated_at=? WHERE id=?",
                (res.username or str(acc.get("login_user") or ""), _now(), _now(), account_id))
            return res.username or ""
        conn.execute("UPDATE publish_accounts SET last_error=?, last_checked_at=? WHERE id=?",
                     (str(res.error or "登录检测未通过")[:200], _now(), account_id))
    return ""


def probe_health(account_id: int) -> Dict[str, Any]:
    """真实校验一次登录态（只读，不改 cookie）。

    这是替代「自动续期」的东西：旧实现开无头浏览器抄一遍 cookie，既拿不到新凭证，
    又会把正常账号写成 expired。现在只问平台「还认不认这个登录态」，
    结果写进 status / last_checked_at / last_error。
    """
    acc = get_account(account_id, include_cookie=True)
    if not acc:
        raise LookupError("账号不存在")
    state = refresh_state(account_id)
    if state.get("running"):
        return {"ok": None, "skipped": "正在登录 / 刷新中", "state": state}

    res = None
    err = ""
    try:
        from . import publish as publish_mod
        driver = publish_mod.get_driver(acc)
        if driver is None:
            return {"ok": None, "skipped": "该平台还没有驱动，无法校验"}
        res = driver.check_auth()
    except Exception as exc:  # noqa: BLE001 - 校验本身失败不等于账号失效
        err = f"{type(exc).__name__}: {exc}"

    now = _now()
    if res is None:
        with write_conn() as conn:
            conn.execute(
                "UPDATE publish_accounts SET last_checked_at=?, last_error=? WHERE id=?",
                (now, ("登录检测失败：" + err)[:250], account_id))
        return {"ok": None, "error": err}

    with write_conn() as conn:
        if res.ok:
            conn.execute(
                "UPDATE publish_accounts SET status='valid', last_checked_at=?,"
                " last_error='', login_user=?, updated_at=? WHERE id=?",
                (now, res.username or str(acc.get("login_user") or ""), now, account_id))
        else:
            conn.execute(
                "UPDATE publish_accounts SET status='expired', last_checked_at=?,"
                " last_error=?, updated_at=? WHERE id=?",
                (now, str(res.error or "登录态已失效")[:250], now, account_id))
    return {"ok": bool(res.ok), "username": res.username, "error": res.error}


def sync_cookie_from_profile(account_id: int) -> List[Dict[str, Any]]:
    """从 profile 里导出一次 cookie 并落库（纯 HTTP 驱动的兜底）。

    Chromium 的 cookie 是加密存在 profile 里的，没法直接读那个 SQLite；
    只能起一个无头浏览器让它自己解密，所以比较重 —— 只在库里的 cookie 为空时调。
    """
    acc = get_account(account_id)
    if not acc:
        raise LookupError("账号不存在")
    if not _HAS_PW:
        raise RuntimeError(PW_HINT)
    if refresh_state(account_id).get("running"):
        raise RuntimeError("这个账号正在登录 / 刷新，等它结束再同步")
    profile = _profile_dir(acc)
    if not profile:
        return []

    from .publish.base import clear_profile_locks, has_running_browser, in_use_error
    pw = sync_playwright().start()
    ctx = None
    try:
        kwargs = _browser_kwargs(acc)
        kwargs["headless"] = True
        try:
            ctx = pw.chromium.launch_persistent_context(str(profile), **kwargs)
        except Exception as exc:  # noqa: BLE001
            if in_use_error(exc) and not has_running_browser(profile):
                clear_profile_locks(profile)
                ctx = pw.chromium.launch_persistent_context(str(profile), **kwargs)
            else:
                raise
        cookies = _safe_cookies(ctx)
        if cookies:
            _save_cookies(account_id, cookies)
        return cookies
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


def _guard_login_session(holder: Dict[str, Any], account_id: int, page: Any) -> bool:
    """等用户扫码：定期重截二维码 + 判登录成功。返回是否成功。"""
    target = _qr_file(account_id)
    ctx = holder.get("context")
    existing = _safe_cookies(ctx)
    before = _cookie_keys(existing)
    deadline = time.time() + _QR_MAX_MINUTES * 60
    last_shot = 0.0

    # profile 里本来就是登录态：不用让人再扫一次，直接把 cookie 同步进库。
    # 判据必须是「有会话类 cookie」且「不在登录页」，否则首页刚打开就种的一堆埋点 cookie 会误判。
    cur_url = _current_url(ctx)
    has_session = any(any(h in str(c.get("name") or "").lower() for h in _LOGIN_COOKIE_HINTS)
                      for c in existing)
    if existing and has_session and cur_url \
            and not any(w in cur_url.lower() for w in _LOGIN_WORDS):
        _save_cookies(account_id, existing)
        _set_state(account_id, phase="verifying", qr_path="", qr_at=0,
                   message="检测到浏览器里已有登录态，正在同步 cookie…")
        return True

    _set_state(account_id, phase="scanning", qr_path="", qr_at=0,
               message="正在生成登录二维码…")

    while time.time() < deadline:
        ctx = holder.get("context")
        if not ctx:
            return False
        if not _browser_alive(ctx):
            _set_state(account_id, running=False, phase="failed",
                       message="登录窗口被关闭了，重新点「扫码登录」即可")
            return False

        # 二维码会过期：按固定间隔重截一次（前端带时间戳取图，能看到刷新）
        if time.time() - last_shot > _QR_REFRESH_SECONDS or not target.exists():
            if _capture_qr(page, target):
                last_shot = time.time()
                # qr_at 给前端当图片缓存键：同一张图不重复拉，刷新后自然换新
                _set_state(account_id, qr_path=str(target), qr_at=int(last_shot),
                           message="请用手机 App 扫描二维码登录（过期会自动刷新）")

        cookies = _safe_cookies(ctx)
        fresh = _fresh_login_cookies(cookies, before)
        cur = _current_url(ctx)
        left_login = bool(cur) and not any(w in cur.lower() for w in _LOGIN_WORDS)
        if fresh and left_login:
            _save_cookies(account_id, cookies)
            _set_state(account_id, phase="verifying", qr_path="",
                       message="扫码成功，正在验证登录态…")
            return True
        time.sleep(2)

    _set_state(account_id, running=False, phase="failed",
               message=f"等了 {_QR_MAX_MINUTES} 分钟还没扫码，已停止。重新点「扫码登录」再试")
    return False


# ---------------------------------------------------------------- 自动续期调度
_SCHED: Dict[str, Any] = {"thread": None, "stop": False}


def _tick_auto() -> None:
    """巡检：按 refresh_hours 间隔真实校验登录态（不再抄 cookie）。

    旧逻辑用 cookie_expire_at 判到期，而头条的会话 cookie 没有过期时间，
    结果每轮都判 due、反复开浏览器空转，还会把正常账号写成 expired。
    现在以「上次校验时间 + 间隔」为准，校验结果只反映真实登录态。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM publish_accounts WHERE enabled=1 AND auto_refresh=1").fetchall()
    for r in rows:
        d = dict(r)
        hours = max(1, int(d.get("refresh_hours") or 12))
        last = str(d.get("last_checked_at") or "")
        due = True
        if last:
            try:
                due = (dt.datetime.now() - dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")) \
                    >= dt.timedelta(hours=hours)
            except ValueError:
                due = True
        if not due or refresh_state(int(d["id"])).get("running"):
            continue
        try:
            probe_health(int(d["id"]))
        except Exception:  # noqa: BLE001 - 单个账号失败不影响其它
            continue


def start_scheduler(interval_minutes: int = 30) -> None:
    if _SCHED["thread"] or not _HAS_PW:
        return

    def loop() -> None:
        while not _SCHED["stop"]:
            time.sleep(max(5, interval_minutes * 60))
            try:
                _tick_auto()
            except Exception:  # noqa: BLE001
                pass

    _SCHED["stop"] = False
    _SCHED["thread"] = threading.Thread(target=loop, daemon=True, name="acct-auto-refresh")
    _SCHED["thread"].start()


def stop_scheduler() -> None:
    _SCHED["stop"] = True
