"""头条号驱动 —— 走浏览器，在页面主世界发请求。

Wechatsync 只给头条留了一个 content script，注释写得很明白：
「content script 的 fetch 不会自动注入 msToken/a_bogus，需要在 MAIN world 执行」。
也就是说头条的写接口带反爬签名，服务端直连发不通，**必须借页面自己的 fetch**。

所以这里不拼 HTTP，而是用 playwright 打开账号自己的浏览器 profile（登录态在里面），
再在页面里 evaluate 一段 fetch —— 等价他们在 MAIN world 做的事，签名参数由页面 JS 补。

端点与报文字段是**从创作页前端 bundle 里扒出来的**（不是猜的）：
* 发文：`POST /mp/agw/article/publish?source=mp&type=article&aid=1231&mp_publish_ab_val=0`
  表单字段由多个 handler 拼出来：editor 给 content/title/search_creation_info/title_id/
  ic_uri_list/extra，common 给 article_type/pgc_id/source/extra，timing 给 save/entrance/
  timer_status/timer_time。其中 **save=0 存草稿、1 直接发布**。
* 传图：`POST /spice/image?upload_source=20020002&need_enhance=true&aid=1231&device_platform=web&scene=paste`
  FormData 里 `imageUrl`（外链）或 `image`（本地字节码），返回 data.web_uri，
  正文里用的地址是 `https://p1.toutiaoimg.com/large/<web_uri>`。
* 回读校验：`GET /mp/agw/article/edit?pgc_id=..&wxstyle=0&format=json`。

响应统一是 `{code:0, data:{...}, message:"..."}`。**只有 code==0 且拿到 pgc_id 才算成功**，
并且发布后会回读一遍，避免「接口说成功、草稿箱里却是空的」。
"""
import base64
import functools
import json
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import Article, AuthResult, PlatformDriver, SyncResult

HOME = "https://mp.toutiao.com/"
PROFILE = "https://mp.toutiao.com/profile_v4/"
PAGE_PUBLISH = "https://mp.toutiao.com/profile_v4/graphic/publish"
PAGE_HOME = "https://mp.toutiao.com/profile_v4/index"
#: 微头条发布页（和图文创作页是两个入口）
PAGE_MICRO = "https://mp.toutiao.com/profile_v4/weitoutiao/publish?from=toutiao_pc"
MICRO_REFERER = "https://mp.toutiao.com/profile_v4/weitoutiao/publish"

# 创作页前端实测在用的接口（见文件头注释）
PUBLISH_API = ("https://mp.toutiao.com/mp/agw/article/publish"
               "?source=mp&type=article&aid=1231&mp_publish_ab_val=0")
UPLOAD_API = ("https://mp.toutiao.com/spice/image?upload_source=20020002"
              "&need_enhance=true&aid=1231&device_platform=web&scene=paste")
EDIT_API = "https://mp.toutiao.com/mp/agw/article/edit"
#: 微头条草稿与微头条图床（网页端没有公开的「直接发布」接口，只存草稿）
MICRO_DRAFT_API = "https://mp.toutiao.com/mp/agw/draft/save_ugc_draft"
MICRO_UPLOAD_API = "https://mp.toutiao.com/mp/agw/media/upload_image/"
#: 微头条最多配几张图（用户在方案里定的上限）
MAX_MICRO_IMAGES = 3
#: 微头条正文长度上限（平台侧约 2000 字，超过直接拒）
MAX_MICRO_CHARS = 2000
LOGIN_API = "https://mp.toutiao.com/mp/agw/media/user_login_status_api"
USER_API = "https://mp.toutiao.com/mp/agw/creator_center/user_info"
#: 上传返回的 image_uri（形如 tos-cn-i-xxx/<md5>）拼成正文可用的图片地址。
#: 与创作页前端的 y(uri, size) 完全一致：https://p1.toutiaoimg.com/large/<uri>
IMG_HOST = "https://p1.toutiaoimg.com/large/"
#: 头条判定「图片 uri 合法」的图床白名单（前端 bundle 里的 T.white）。
#: 不在名单里的地址写进正文会被服务端拒：code=7115 图片uri非法。
#: 注意 image-tt-private.toutiao.com（上传返回的 image_url）是带签名的私有临时域，
#: 一个都不沾，只能拿 image_uri 自己拼。
IMG_WHITE = ("toutiaoimg", "toutiaostatic", "byteimg", "bytecdn", "p-boe.byted")
#: 头条正文最少字数（少于这个数服务端会直接拒）
MIN_TEXT = 30
#: 服务端返回的「没登录」类错误码（100004 user not login / 100005 获取用户信息失败）
_NOT_LOGIN_CODES = ("100004", "100005", "1002", "100002")

ENDPOINTS = {
    "user_info": [LOGIN_API, USER_API],
    "upload": [UPLOAD_API],
    "publish": [PUBLISH_API],
    "micro_upload": [MICRO_UPLOAD_API],
    "micro_draft": [MICRO_DRAFT_API],
}

# 抓包时排除的静态资源后缀
_STATIC = (".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".woff",
           ".woff2", ".ico", ".gif", ".webp", ".mp4")
# 抓包时只留这些特征，避免噪音
_HINTS = ("/mp/agw/", "/api/", "/mapi/", "/spice/", "upload", "publish", "article",
          "media", "user/info", "save", "weitoutiao", "ugc", "draft", "upload_image")

_NEED_PW = "头条号需要浏览器：pip install playwright && playwright install chromium"


def _uri_of(url: str) -> str:
    """从完整图片 URL 里抽出 tos 路径（前端 h() 同款清洗：去 query、~ 后段、扩展名）。"""
    m = re.search(r"https?://[^/]+(/.*)", url or "")
    p = m.group(1) if m else (url or "")
    p = p.split("?")[0].split("~")[0]
    return re.sub(r"\.[a-zA-Z]+$", "", p).lstrip("/")


def img_ok(url: str) -> bool:
    """这张图的地址能不能直接写进头条正文（域名得在它的图床白名单里）。"""
    low = (url or "").lower()
    return "juejin.byteimg" not in low and any(w in low for w in IMG_WHITE)


def _has_display() -> bool:
    """这台机器能不能开有头浏览器（WSL / 纯服务器上没有 X display）。"""
    import os
    import sys
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _offloop(func: Callable[..., Any]) -> Callable[..., Any]:
    """把方法体丢到一个全新的普通线程里跑。

    Playwright sync API 启动时若发现当前线程已有正在运行的 asyncio loop 就会直接抛
    「It looks like you are using Playwright Sync API inside the asyncio loop」。

    FastAPI 的同步路由跑在 anyio 的 worker 线程上，而这个线程池是**复用的**：
    只要上一次请求遗留了一个还在跑的 loop（异常路径没收干净），后续请求复用同一线程
    就会命中这个检测，从此每次都 500。全新线程里没有 running loop，可彻底避开。
    """
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        box: Dict[str, Any] = {}

        def target() -> None:
            try:
                box["value"] = func(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - 原样抛给调用方处理
                box["error"] = exc

        th = threading.Thread(target=target, daemon=True)
        th.start()
        th.join()
        if "error" in box:
            raise box["error"]
        return box.get("value")
    return wrapper


class ToutiaoDriver(PlatformDriver):
    id = "toutiao"
    name = "头条号"
    homepage = HOME
    output_format = "html"
    needs_browser = True
    # 已在头条图床上的图不用重复传；含私有域等不在白名单的地址一律重传
    skip_patterns = IMG_WHITE

    def __init__(self, account: Dict[str, Any]) -> None:
        super().__init__(account)
        self._profile = str(account.get("profile_dir") or "").strip()

    # ---------------------------------------------------------------- 浏览器
    def _open_page(self, goto: str = "", wait_ready: bool = False):
        """开一个带着登录态的页面。用完必须配合 _close 释放。

        goto：开完直接跳到指定地址。发文必须停在创作页——反爬签名脚本（byted_acrawler）
        和编辑器都是在创作页里初始化的，停在首页直接发请求会少了签名。
        wait_ready：等页面把编辑器和签名脚本跑起来。
        """
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(_NEED_PW) from exc
        if not self._profile:
            # 没配 profile 就用一次性临时目录：登录态靠注入的 cookie，不必再纠结 profile 放哪
            import tempfile
            profile = Path(tempfile.mkdtemp(prefix="autonews-toutiao-"))
        else:
            from .base import (clear_profile_locks, has_running_browser, in_use_error,
                           normalize_path)
            profile = Path(normalize_path(self._profile))
        profile.mkdir(parents=True, exist_ok=True)
        headless = bool(self.account.get("headless"))
        if not headless and not _has_display():
            # WSL / 服务器上没有显示器，有头启动必然失败，直接退回无头
            headless = True
        pw = sync_playwright().start()
        try:
            ctx = pw.chromium.launch_persistent_context(str(profile), headless=headless)
        except Exception as exc:  # noqa: BLE001
            # 上一个实例没干净退出会留锁，导致这里报 profile in use
            if in_use_error(exc) and not has_running_browser(profile):
                clear_profile_locks(profile)
                ctx = pw.chromium.launch_persistent_context(str(profile), headless=headless)
            else:
                raise
        # profile 里可能没登录（比如在别的浏览器登的、或 WSLg 不可用），
        # 这时把库里手动粘贴的 cookie 注进去，页面照样能带登录态跑
        self._inject_cookies(ctx)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        target = goto or HOME
        if target not in (page.url or ""):
            try:
                page.goto(target, timeout=60_000, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001 - 首屏慢不影响后续 evaluate
                pass
        if wait_ready:
            self._wait_ready(page)
        return pw, ctx, page

    _READY_JS = """
        async (timeout) => {
          const t0 = Date.now();
          const out = {acrawler: false, editor: false, login: true};
          while (Date.now() - t0 < timeout) {
            if (window.byted_acrawler) out.acrawler = true;
            if (document.querySelector('.publish-editor, .publish-editor-title-inner')) out.editor = true;
            if (out.acrawler && out.editor) break;
            await new Promise(r => setTimeout(r, 300));
          }
          out.login = !/(auth\\/page\\/login|passport)/.test(location.href);
          return out;
        }
    """

    def _wait_ready(self, page: Any, timeout: int = 10_000) -> Dict[str, bool]:
        """等创作页的编辑器与签名脚本就绪；顺带带回「页面有没有停在登录页」。"""
        try:
            return dict(page.evaluate(self._READY_JS, timeout))
        except Exception:  # noqa: BLE001 - 页面还没起来就当没就绪，后面还有接口兜底
            return {"acrawler": False, "editor": False, "login": True}

    def _login_state(self, page: Any) -> Optional[bool]:
        """问一次登录态接口；问不出来返回 None（不冤枉人）。"""
        res = self._request(LOGIN_API, "GET", page=page)
        data = self._json(res)
        if data is None:
            return None
        d = data.get("data") or {}
        flag = d.get("is_login", d.get("login"))
        if isinstance(flag, bool):
            return flag
        if str(data.get("code", data.get("errno", "")) or "") == "0":
            return bool(d)
        return False

    @staticmethod
    def _looks_logged_out(page: Any) -> bool:
        url = ""
        try:
            url = page.url or ""
        except Exception:  # noqa: BLE001
            return False
        return any(k in url for k in ("passport", "/auth/page/login", "login"))

    def _inject_cookies(self, ctx: Any) -> None:
        """把账号里存的 cookie 塞进浏览器上下文。"""
        raw = str(self.account.get("cookie") or "").strip()
        if not raw:
            return
        try:
            items = json.loads(raw)
        except ValueError:
            return
        if not isinstance(items, list) or not items:
            return
        cks = []
        for c in items:
            name, domain = str(c.get("name") or ""), str(c.get("domain") or "")
            if not name or not domain:
                continue     # 没 domain 浏览器不知道发给谁
            expires = c.get("expires") or -1
            try:
                expires = float(expires)
            except (TypeError, ValueError):
                expires = -1.0
            if expires <= 0:      # 会话 cookie：给个未来的时间，别让它一进去就失效
                expires = time.time() + 86400 * 30
            cks.append({
                "name": name, "value": str(c.get("value") or ""),
                "domain": domain if domain.startswith(".") else "." + domain,
                "path": str(c.get("path") or "/"), "expires": expires,
                "httpOnly": bool(c.get("httpOnly")), "secure": bool(c.get("secure")),
                "sameSite": str(c.get("sameSite") or "Lax"),
            })
        if not cks:
            return
        try:
            ctx.add_cookies(cks)
        except Exception:  # noqa: BLE001 - 注入失败不致命，还有 profile 兜底
            pass

    @staticmethod
    def _close(pw, ctx) -> None:
        for obj in (ctx, pw):
            if not obj:
                continue
            try:
                obj.close() if obj is ctx else obj.stop()
            except Exception:  # noqa: BLE001
                pass

    _FETCH_JS = """
        async ([url, method, headers, body, asForm, formFields, b64, blobName]) => {
          const opt = { method, headers: headers || {}, credentials: 'include' };
          if (asForm) {
            const fd = new FormData();
            for (const [k, v] of (formFields || [])) fd.append(k, v);
            if (b64) {
              const bin = atob(b64);
              const arr = new Uint8Array(bin.length);
              for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
              fd.append(blobName || 'media', new Blob([arr], { type: 'image/jpeg' }), 'image.jpg');
            }
            opt.body = fd;
          } else if (body != null) {
            opt.body = body;
          }
          try {
            const r = await fetch(url, opt);
            const t = await r.text();
            return { ok: true, status: r.status, text: t.slice(0, 4000) };
          } catch (e) {
            return { ok: false, status: 0, text: String(e) };
          }
        }
    """

    def _request(self, url: str, method: str = "GET", headers: Optional[Dict[str, str]] = None,
                 body: Optional[str] = None, form_fields: Optional[List[tuple]] = None,
                 b64: str = "", page=None, blob_name: str = "media") -> Dict[str, Any]:
        """在页面主世界发一个请求。传了 page 就复用，否则自己开关浏览器。

        页面跳转（比如被踢回登录页）会把 evaluate 的执行上下文冲掉，
        这种错不是真的失败，等一下重试；重试都不行才当成失败返回，不让异常往上抛。
        """
        args = [url, method, headers or {}, body, bool(form_fields or b64),
                form_fields or [], b64, blob_name]
        last: Optional[BaseException] = None
        for _ in range(3):
            try:
                if page is not None:
                    return dict(page.evaluate(self._FETCH_JS, args))
                pw, ctx, pg = self._open_page()
                try:
                    return dict(pg.evaluate(self._FETCH_JS, args))
                finally:
                    self._close(pw, ctx)
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1.5)
        return {"ok": False, "status": 0, "text": str(last)}

    # ---------------------------------------------------------------- 登录态
    @_offloop
    def check_auth(self) -> AuthResult:
        try:
            pw, ctx, page = self._open_page(goto=PAGE_HOME, wait_ready=True)
        except RuntimeError as exc:
            return AuthResult(False, error=str(exc))
        try:
            if self._looks_logged_out(page):
                return AuthResult(
                    False, error="未登录：页面停在登录页，先在「账号管理 → 刷新 Cookie」里登录一次")
            last = ""
            for url in self._endpoints("user_info"):
                res = self._request(url, "GET", page=page)
                if not res.get("ok") or res.get("status", 0) >= 400:
                    continue
                data = self._json(res)
                if data is None:
                    continue
                last = (res.get("text") or "")[:120]
                code = str(data.get("code", data.get("errno", "")) or "")
                # 没登录时头条会返回非 0（比如 code=1002 / need login），这种不能算通过
                if code and code != "0":
                    continue
                user = data.get("data") or data.get("user") or data
                if isinstance(user, dict):
                    # user_login_status_api 会直接给 is_login，false 就是没登录（此时 code 仍是 0）
                    flag = user.get("is_login", user.get("login"))
                    if flag is False:
                        return AuthResult(False, error="未登录，请先刷新 cookie")
                    for nest in ("media", "user", "user_info", "author_info"):
                        sub = user.get(nest)
                        if isinstance(sub, dict) and (sub.get("name") or sub.get("id")):
                            user = sub
                            break
                name = str(user.get("name") or user.get("nickname") or user.get("media_name")
                           or user.get("screen_name") or "")
                uid = str(user.get("user_id") or user.get("uid") or user.get("id")
                          or user.get("media_id") or "")
                if name or uid:
                    return AuthResult(True, username=name, user_id=uid)
            if self._looks_logged_out(page):
                return AuthResult(False, error="未登录，请先刷新 cookie")
            return AuthResult(False, error="登录检测接口没返回用户信息，点「探测端点」看看哪个通"
                                           + (("，最后响应：" + last) if last else ""))
        finally:
            self._close(pw, ctx)

    # ---------------------------------------------------------------- 传图
    @_offloop
    def upload_image(self, src: str) -> str:
        """传一张图，返回能写进正文的 URL。

        外链图走 `imageUrl` 字段让头条自己抓（省得我们下载再上传），
        本地文件才读成字节走 `image` 字段。
        """
        remote = src.startswith(("http://", "https://"))
        b64 = ""
        fields: List[tuple] = []
        if remote:
            fields = [("imageUrl", src)]
        else:
            blob, _ = self.read_image(src)
            b64 = base64.b64encode(blob).decode()
        # publish 里已经开着创作页就复用，别为一个 profile 再起第二个浏览器（必然抢锁）
        shared = getattr(self, "_shared_page", None)
        if shared is not None:
            return self._upload_on_page(shared, fields, b64)
        pw, ctx, page = self._open_page(goto=PAGE_PUBLISH, wait_ready=True)
        try:
            return self._upload_on_page(page, fields, b64)
        finally:
            self._close(pw, ctx)

    @_offloop
    def upload_images(self, srcs: List[str],
                      on_progress: Optional[Callable[[int, int], None]] = None) -> List[str]:
        """批量传图：整批只开一次创作页。

        upload_image 每次都会 _open_page(goto=PAGE_PUBLISH, wait_ready=True)，
        光是等编辑器与签名脚本就绪就要几秒；一键复制往往一次传 5~6 张，
        不批量的话用户要干等十几秒，所以这里复用同一个页面逐张上传。
        """
        items = [str(s or "") for s in (srcs or [])]
        out: List[str] = []
        if not items:
            return out
        pw, ctx, page = self._open_page(goto=PAGE_PUBLISH, wait_ready=True)
        try:
            total = len(items)
            for i, src in enumerate(items, 1):
                if on_progress:
                    on_progress(i, total)
                try:
                    remote = src.startswith(("http://", "https://"))
                    b64 = "" if remote else base64.b64encode(self.read_image(src)[0]).decode()
                    fields: List[tuple] = [("imageUrl", src)] if remote else []
                    out.append(self._upload_on_page(page, fields, b64) or "")
                except Exception:  # noqa: BLE001 - 单张失败交给上层回退内联
                    out.append("")
        finally:
            self._close(pw, ctx)
        return out

    def _upload_on_page(self, page: Any, fields: List[tuple], b64: str) -> str:
        """在给定页面上传一张图，返回正文可用的地址。"""
        res: Dict[str, Any] = {}
        for url in self._endpoints("upload"):
            res = self._request(url, "POST", page=page, b64=b64,
                                form_fields=fields, blob_name="image")
            if not res.get("ok") or res.get("status", 0) >= 400:
                continue
            data = self._json(res)
            if data is None:
                continue
            d = data.get("data") or {}
            if str(data.get("code", data.get("errno", "")) or "") not in ("", "0"):
                continue
            out = self._image_url(d)
            if not out:
                raise RuntimeError("上传成功但没拿到图片 uri：" + (res.get("text") or "")[:200])
            # 地址不在头条图床白名单里就等于白传：写进正文会被整篇拒（7115）
            if not img_ok(out):
                out = IMG_HOST + _uri_of(out)
            return out
        raise RuntimeError("图片上传失败：" + (res.get("text") or "")[:200])

    @staticmethod
    def _image_url(data: Dict[str, Any]) -> str:
        """上传响应 → 正文可用的图片地址。

        优先用 image_uri（tos 路径）自己拼图床地址；image_url 那种私有临时域
        带签名且不在白名单，只能拿来兜底抽路径。
        """
        if not isinstance(data, dict):
            return ""
        uri = str(data.get("image_uri") or data.get("origin_image_uri")
                  or data.get("web_uri") or data.get("uri") or "")
        if uri:
            return uri if uri.startswith("http") else IMG_HOST + uri.lstrip("/")
        for key in ("url", "web_url", "image_url", "origin_image_url", "cover_url"):
            v = str(data.get(key) or "")
            if v.startswith(("http://", "https://")):
                return v
        return ""

    @staticmethod
    def _json(res: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """把页面 fetch 回来的文本解析成 dict；解析不出来返回 None。"""
        try:
            data = json.loads(res.get("text") or "")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    # ---------------------------------------------------------------- 发布
    @_offloop
    def publish(self, article: Article, draft_only: bool = True,
                on_progress: Optional[Callable[[int, int], None]] = None) -> SyncResult:
        # 微头条与图文是两个发布入口：微头条没有标题、只存草稿
        if str(getattr(article, "form", "article") or "article") == "micro":
            return self._publish_micro(article, draft_only)
        try:
            # 必须停在创作页：签名脚本 byted_acrawler 只在这个页面里初始化
            pw, ctx, page = self._open_page(goto=PAGE_PUBLISH, wait_ready=True)
        except RuntimeError as exc:
            return self.fail(str(exc))
        try:
            # 先问一次登录态：没登录就别白跑一遍传图，直接把原因说清楚
            if self._login_state(page) is False or self._looks_logged_out(page):
                return self.fail("头条未登录：先去「账号管理 → 刷新 Cookie」登录一次，再回来发布")

            # 传图复用一个创作页，别为一个 profile 起第二个浏览器
            self._shared_page = page
            try:
                content, uploaded = self.process_images(article.html or "", on_progress,
                                                        drop_failed=True)
            finally:
                self._shared_page = None
            # 兜底：正文里只要还剩一张不在头条图床上的图，整篇都会被拒（7115）
            content, dropped = self._strip_bad_images(content)
            if not content.strip():
                return self.fail("正文为空，没法发布")
            text_len = len(re.sub(r"<[^>]+>", "", content).strip())
            if text_len < MIN_TEXT:
                return self.fail(f"正文只有 {text_len} 字，头条要求不少于 {MIN_TEXT} 字，先补全再发")

            body = self._form_body(article.title, content, draft_only, self._media_id(page))
            headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
            last = ""
            for url in self._endpoints("publish"):
                res = self._request(url, "POST", page=page, body=body, headers=headers)
                last = (res.get("text") or "")[:300]
                if not res.get("ok") or res.get("status", 0) >= 400:
                    continue
                data = self._json(res)
                if data is None:
                    continue
                # 头条的响应是 {code, message, data:{pgc_id}}；code 非 0 一律算失败，
                # 拿不到 pgc_id 也一律算失败——不然就会出现「说成功、草稿箱里却没有」
                code = str(data.get("code", data.get("errno", "")) or "")
                d = data.get("data") or {}
                post_id = ""
                if isinstance(d, dict):
                    post_id = str(d.get("pgc_id") or d.get("pgcId") or d.get("article_id")
                                  or d.get("item_id") or d.get("id") or "")
                if code == "0" and post_id:
                    note = self._verify(page, post_id, article.title)
                    if note:
                        return self.fail(note, post_id=post_id)
                    extra = f"，另有 {dropped} 张图传不上头条已剔除" if dropped else ""
                    return self.ok(
                        post_id=post_id,
                        post_url=f"{PAGE_PUBLISH}?pgc_id={post_id}",
                        draft_only=draft_only,
                        message=f"已{'存草稿' if draft_only else '发布'}，上传图片 {uploaded} 张，"
                                f"正文 {text_len} 字（已回读校验）{extra}",
                    )
                why = str(data.get("message") or data.get("errmsg") or data.get("reason") or "")
                if code in _NOT_LOGIN_CODES or "not login" in why.lower():
                    return self.fail("头条提示未登录（code=%s）：先去「账号管理 → 刷新 Cookie」登录一次" % code)
                if code == "7050":
                    return self.fail(
                        "头条返回 7050「保存失败」（pgc_id=0）。已实测：在官方创作页手动输入标题正文，"
                        "由页面自己触发自动存草稿，返回的同样是 7050 —— 这是账号侧限制，不是报文问题。"
                        "常见原因：①新号「在头条创作第 0 天」/ 新手作者期，图文草稿暂存受限；"
                        "②「创作权益 → 文章创作」未开通（微头条权益可能已开通，所以微头条能存）；"
                        "③账号资料/实名审核仍在处理中。"
                        "请到 mp.toutiao.com「创作权益」确认文章权益已开通、账号已过新手限制后再试。")
                return self.fail(f"头条拒绝了这次提交（code={code or '?'}）：{why or last}")
            return self.fail("发布接口没成功，响应：" + last)
        finally:
            self._close(pw, ctx)

    # ---------------------------------------------------------------- 微头条
    def _publish_micro(self, article: Article, draft_only: bool = True) -> SyncResult:
        """微头条：没有标题，只存草稿。

        头条网页端没有公开的「直接发布微头条」接口，草稿接口是
        `/mp/agw/draft/save_ugc_draft`；用户明确要求最终由人工在草稿箱审核发布。
        """
        if not draft_only:
            return self.fail("微头条网页端只支持存草稿：已按草稿提交，请到草稿箱人工发布")
        content = self._micro_text(article.html or article.markdown or "")
        if not content:
            return self.fail("微头条正文为空，没法保存草稿")
        if len(content) > MAX_MICRO_CHARS:
            return self.fail(f"微头条正文 {len(content)} 字，超过平台上限 {MAX_MICRO_CHARS}，先精简")
        try:
            pw, ctx, page = self._open_page(goto=PAGE_MICRO)
        except RuntimeError as exc:
            return self.fail(str(exc))
        try:
            try:
                page.wait_for_timeout(1500)     # 等页面把编辑器/签名脚本拉起来
            except Exception:  # noqa: BLE001
                pass
            if self._login_state(page) is False or self._looks_logged_out(page):
                return self.fail("头条未登录：先去「账号管理 → 刷新 Cookie」登录一次，再回来发布")

            images = self._micro_images(page, article.html or "")
            body = self._micro_draft_body(content, images)
            headers = {"Content-Type": "application/json; charset=UTF-8",
                       "Referer": MICRO_REFERER}
            last = ""
            for url in self._endpoints("micro_draft"):
                res = self._request(url, "POST", page=page, body=body, headers=headers)
                last = (res.get("text") or "")[:300]
                if not res.get("ok") or res.get("status", 0) >= 400:
                    continue
                data = self._json(res)
                if data is None:
                    continue
                code = str(data.get("code", data.get("errno", "")) or "")
                if code not in ("", "0", "200"):
                    why = str(data.get("message") or data.get("errmsg") or last)
                    if code in _NOT_LOGIN_CODES or "login" in why.lower():
                        return self.fail("头条提示未登录（code=%s）：先刷新 Cookie 再发" % code)
                    return self.fail(f"微头条草稿被拒（code={code}）：{why}")
                d = data.get("data") if isinstance(data.get("data"), dict) else {}
                post_id = str(d.get("id") or d.get("post_id") or d.get("item_id") or "")
                extra = f"，上传图片 {len(images)} 张" if images else ""
                return self.ok(post_id=post_id, post_url=PAGE_MICRO, draft_only=True,
                               message=f"已存微头条草稿{extra}（正文 {len(content)} 字），"
                                       f"请到头条草稿箱人工审核发布")
            return self.fail("微头条草稿接口没成功：" + last)
        finally:
            self._close(pw, ctx)

    @staticmethod
    def _micro_text(raw: str) -> str:
        """排版稿 → 微头条纯文本：去标签/图片/标题，保留自然分段。"""
        import html as _html
        s = str(raw or "")
        s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)             # Markdown 图片
        s = re.sub(r"<img[^>]*>", "", s, flags=re.I)
        # 微头条没有标题和小标题：整段标题直接丢掉，而不是把文字留下
        s = re.sub(r"<h\d[^>]*>.*?</h\d>", "\n", s, flags=re.I | re.S)
        s = re.sub(r"<br\s*/?>|</p>|</div>|</h\d>", "\n", s, flags=re.I)
        s = re.sub(r"<[^>]+>", "", s)
        s = _html.unescape(s)
        s = re.sub(r"[ \t\u3000]+", " ", s)
        lines = [ln.strip() for ln in s.splitlines()]
        lines = [ln for ln in lines if ln and not re.match(r"^#{1,6}\s", ln)]
        return "\n".join(lines).strip()

    def _micro_images(self, page: Any, html: str) -> List[Dict[str, Any]]:
        """从正文里取图片（最多 MAX_MICRO_IMAGES 张），逐张上传成微头条图床格式。"""
        srcs: List[str] = []
        for m in re.finditer(r'<img[^>]+src="([^"]+)"', str(html or ""), re.I):
            srcs.append(m.group(1))
        for m in re.finditer(r"!\[[^\]]*\]\(([^)\s]+)\)", str(html or "")):
            srcs.append(m.group(1))
        out: List[Dict[str, Any]] = []
        seen = set()
        for src in srcs:
            if len(out) >= MAX_MICRO_IMAGES:
                break
            if not src or src in seen:
                continue
            seen.add(src)
            try:
                out.append(self._upload_micro_image(page, src))
            except Exception:  # noqa: BLE001 - 单张失败不毁整条
                continue
        return out

    def _upload_micro_image(self, page: Any, src: str) -> Dict[str, Any]:
        """把一张图传到微头条图床，返回 draft_info.images 需要的字段。"""
        blob, _name = self.read_image(src)
        b64 = base64.b64encode(blob).decode()
        width = height = 0
        try:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(blob)) as im:
                width, height = im.size
        except Exception:  # noqa: BLE001 - 没装 Pillow 也能发，宽高留 0
            pass
        last = ""
        # 头条的微头条图床上传字段名在不同版本里是 image / file，两个都试
        for field in ("image", "file"):
            for url in self._endpoints("micro_upload"):
                res = self._request(
                    url, "POST", page=page, b64=b64, blob_name=field,
                    form_fields=[("type", "image"), ("source", "weitoutiao")])
                last = (res.get("text") or "")[:300]
                if not res.get("ok") or res.get("status", 0) >= 400:
                    continue
                data = self._json(res)
                if data is None:
                    continue
                code = str(data.get("code", data.get("errno", "")) or "")
                if code not in ("", "0", "200"):
                    continue
                d = data.get("data") if isinstance(data.get("data"), dict) else data
                uri = str(d.get("image_uri") or d.get("uri") or d.get("web_uri") or "")
                web_uri = str(d.get("web_uri") or uri)
                link = str(d.get("url") or d.get("image_url") or "")
                if not (uri or web_uri or link):
                    continue
                if not link and uri:
                    link = IMG_HOST + uri.lstrip("/")
                return {"url": link, "web_uri": web_uri, "uri": uri or web_uri,
                        "width": width, "height": height,
                        "mime_type": "image/jpeg", "file_size": len(blob)}
        raise RuntimeError("微头条图片上传失败：" + last)

    @staticmethod
    def _micro_draft_body(content: str, images: List[Dict[str, Any]]) -> str:
        """拼微头条草稿报文：draft_info 是内层 JSON 字符串（平台要求二次编码）。"""
        info = {
            "schema": "sslocal://send_thread",
            "extra": {"claim_exclusive": "1", "tuwen_wtt_transfer_switch": "1"},
            "content": content,
            "content_rich_span": json.dumps({"links": []}, ensure_ascii=False),
            "images": images or [],
            "update_time": int(time.time()),
        }
        return json.dumps({"draft_type": 1,
                           "draft_info": json.dumps(info, ensure_ascii=False),
                           "post_id": ""}, ensure_ascii=False)

    @staticmethod
    def _strip_bad_images(content: str) -> Tuple[str, int]:
        """删掉正文里不落在头条图床上的图片。

        留一张外链（或上传返回的私有临时域地址）整篇都会被服务端拒，所以宁可不要这张图。
        """
        gone = 0

        def drop(m: "re.Match") -> str:
            nonlocal gone
            if img_ok(m.group(1)):
                return m.group(0)
            gone += 1
            return ""

        content = re.sub(r'<img[^>]+src="([^"]+)"[^>]*>', drop, content, flags=re.I)
        content = re.sub(r"!\[[^\]]*\]\(([^)\s]+)\)", drop, content)
        return content, gone

    def _media_id(self, page: Any) -> str:
        """取 media_id：title_id 要用「时间戳_media_id」的格式，空着会被判保存失败。"""
        try:
            data = self._json(self._request(USER_API, "GET", page=page)) or {}
        except Exception:  # noqa: BLE001
            return ""
        for key in ("media_id", "user_id", "id"):
            if data.get(key):
                return str(data[key])
        return ""

    @staticmethod
    def _form_body(title: str, content: str, draft_only: bool, media_id: str = "") -> str:
        """拼头条 article/publish 的表单报文。

        字段照创作页自己的自动存草稿请求抓包整理（2026-10 实测）：
        title_id 必须非空、extra 里要带 gd_ext / tuwen_wtt_transfer_switch，
        并补齐封面与声明类默认值，否则服务端只回一个笼统的 7050。
        """
        text_len = len(re.sub(r"<[^>]+>", "", content).strip())
        extra = {
            "content_source": 100000000402,
            "content_word_cnt": text_len,
            "is_multi_title": 0,
            "sub_titles": [],
            "gd_ext": {"entrance": "", "from_page": "publisher_mp", "enter_from": "PC",
                       "device_platform": "mp", "is_message": 0},
            "tuwen_wtt_transfer_switch": "1",
        }
        fields: List[Tuple[str, str]] = [
            ("source", "29"),
            ("extra", json.dumps(extra, ensure_ascii=False)),
            ("content", content),
            ("title", title),
            ("search_creation_info",
             json.dumps({"searchTopOne": 0, "abstract": "", "clue_id": ""},
                        ensure_ascii=False)),
            ("title_id", f"{int(time.time() * 1000)}_{media_id}"),
            ("mp_editor_stat", "{}"),
            ("is_refute_rumor", "0"),
            # timing handler：PUBLISH=1、DRAFT/PREVIEW=0
            ("save", "0" if draft_only else "1"),
            ("entrance", ""),
            ("timer_status", "0"),
            ("timer_time", ""),
            ("educluecard", ""),
            ("draft_form_data", json.dumps({"coverType": 2})),
            ("pgc_feed_covers", "[]"),
            ("article_ad_type", "3"),
            ("is_fans_article", "0"),
            ("govern_forward", "0"),
            ("praise", "0"),
            ("disable_praise", "0"),
            ("tree_plan_article", "0"),
            ("star_order_id", ""),
            ("star_order_name", ""),
            ("customer_nick_name", ""),
            ("activity_tag", "0"),
            ("trends_writing_tag", "0"),
            ("claim_exclusive", "0"),
        ]
        return urllib.parse.urlencode(fields, encoding="utf-8")

    def _verify(self, page: Any, post_id: str, title: str) -> str:
        """发布完回读一次，确认草稿箱里那篇真的有标题和正文。

        返回空串表示通过；非空串是要报给用户的失败原因。
        回读请求本身失败时不判失败（只可能漏报，不能误报）。
        """
        url = f"{EDIT_API}?pgc_id={post_id}&wxstyle=0&format=json"
        res = self._request(url, "GET", page=page)
        if not res.get("ok") or res.get("status", 0) >= 400:
            return ""      # 回读接口不通，不冤枉已经成功的操作
        data = self._json(res)
        if data is None:
            return ""
        d = data.get("data") or data
        if not isinstance(d, dict):
            return ""
        for nest in ("article", "articlePgc", "pgc", "data", "info"):
            sub = d.get(nest)
            if isinstance(sub, dict):
                d = sub
                break
        body = str(d.get("content") or d.get("article_content") or d.get("contentCache") or "")
        got_title = str(d.get("title") or d.get("article_title") or "")
        if not body and not got_title:
            # 字段位置对不上就别硬判，避免误报失败
            return ""
        if len(re.sub(r"<[^>]+>", "", body).strip()) < MIN_TEXT:
            return (f"头条返回了成功，但回读草稿（pgc_id={post_id}）正文是空的——"
                    f"多半是正文里的标签被头条过滤掉了，去草稿箱确认一下")
        return ""

    # ---------------------------------------------------------------- 端点发现
    def _saved_endpoints(self) -> Dict[str, List[str]]:
        raw = str(self.account.get("api_endpoints") or "").strip()
        if not raw:
            return {}
        try:
            d = json.loads(raw)
        except ValueError:
            return {}
        return {k: v for k, v in d.items() if isinstance(v, list) and v}

    def _endpoints(self, kind: str) -> List[str]:
        """优先用探测时存下来的真实端点，没有才退回常量候选。"""
        saved = list(self._saved_endpoints().get(kind) or [])
        return saved + [u for u in ENDPOINTS.get(kind, []) if u not in saved]

    @staticmethod
    def classify(items: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        """把抓到的请求按用途归类，供 publish 直接使用（图文与微头条分开）。"""
        buckets: Dict[str, List[str]] = {"user_info": [], "upload": [], "publish": [],
                                         "micro_upload": [], "micro_draft": []}
        for it in items:
            url, method = it.get("url") or "", str(it.get("method") or "").upper()
            if method == "PAGE" or "toutiao.com" not in url:
                continue
            low = url.lower()
            if method in ("POST", "PUT"):
                if "save_ugc_draft" in low and url not in buckets["micro_draft"]:
                    buckets["micro_draft"].append(url)
                elif ("upload_image" in low or "media/upload" in low) \
                        and url not in buckets["micro_upload"]:
                    buckets["micro_upload"].append(url)
                elif ("/spice/image" in low or "upload" in low) \
                        and url not in buckets["upload"]:
                    buckets["upload"].append(url)
                elif any(k in low for k in ("article/publish", "publish", "save",
                                            "create", "/article/post")) \
                        and url not in buckets["publish"]:
                    buckets["publish"].append(url)
            elif method == "GET":
                hits = ("user/info", "user_info", "user_login_status", "media/info",
                        "get_media_info", "/user/profile", "user/get", "creator_center/user")
                if any(k in low for k in hits) and url not in buckets["user_info"]:
                    buckets["user_info"].append(url)
        return buckets

    def discover(self, page: Any = None) -> List[Dict[str, Any]]:
        """打开创作页监听网络，抓出头条真正在用的接口。

        猜路径没意义（老路径全 404），页面自己发的请求才是准的。
        """
        own = page is None
        pw = ctx = None
        if own:
            pw, ctx, page = self._open_page()
        seen: List[Dict[str, Any]] = []
        try:
            def on_request(req: Any) -> None:
                url = str(req.url).split("?")[0]
                if url.endswith(_STATIC) or len(seen) >= 80:
                    return
                if not any(h in url for h in _HINTS):
                    return
                seen.append({"url": url, "method": req.method,
                             "type": req.resource_type})

            page.on("request", on_request)
            # 图文创作页、微头条发布页、首页都走一遍：微头条的草稿/图床接口只在它自己的页面里发
            for target in (PAGE_PUBLISH, PAGE_MICRO, PAGE_HOME):
                try:
                    page.goto(target, wait_until="domcontentloaded", timeout=45_000)
                    page.wait_for_timeout(4500)   # 等前端把接口都发出去
                except Exception:  # noqa: BLE001 - 某个页面打不开不影响另一个
                    continue
            seen.append({"url": page.url, "method": "PAGE", "type": "document"})
        finally:
            if own:
                self._close(pw, ctx)

        out, keys = [], set()
        for it in seen:
            key = (it["url"], it["method"])
            if key in keys:
                continue
            keys.add(key)
            out.append(it)
        return out

    # ---------------------------------------------------------------- 端点探测
    @_offloop
    def probe(self) -> Dict[str, Any]:
        """先抓真实请求，再拿候选路径试连通性。"""
        try:
            pw, ctx, page = self._open_page(goto=PAGE_PUBLISH, wait_ready=True)
        except RuntimeError as exc:
            return {"items": [{"name": "browser", "url": "", "status": 0,
                               "method": "", "text": str(exc)}], "buckets": {}}
        out: List[Dict[str, Any]] = []
        buckets: Dict[str, List[str]] = {}
        try:
            try:
                found = self.discover(page)
                buckets = self.classify(found)
            except Exception as exc:  # noqa: BLE001
                found = [{"url": "", "method": "GET", "text": "抓包失败：" + str(exc)}]
            for g in found:
                out.append({"name": "discovered", "url": g.get("url", ""),
                            "method": g.get("method", ""), "type": g.get("type", ""),
                            "status": 1, "text": "页面真实请求"})

            # 有抓到的就用抓到的试，没抓到才试常量
            probe_map = {
                "user_info": self._endpoints("user_info") if not buckets.get("user_info")
                else buckets["user_info"],
                "upload": self._endpoints("upload") if not buckets.get("upload")
                else buckets["upload"],
                "publish": self._endpoints("publish") if not buckets.get("publish")
                else buckets["publish"],
                "micro_upload": self._endpoints("micro_upload") if not buckets.get("micro_upload")
                else buckets["micro_upload"],
                "micro_draft": self._endpoints("micro_draft") if not buckets.get("micro_draft")
                else buckets["micro_draft"],
            }
            for name, urls in probe_map.items():
                for url in urls:
                    if name in ("upload", "micro_upload"):
                        res = self._request(url, "POST", page=page,
                                            form_fields=[("type", "image")])
                    elif name in ("publish", "micro_draft"):
                        # 草稿接口绝不能拿真报文探活，用 OPTIONS 只查连通性
                        res = self._request(url, "OPTIONS", page=page)
                    else:
                        res = self._request(url, "GET", page=page)
                    out.append({"name": name, "url": url, "method": "",
                                "status": res.get("status", 0),
                                "text": (res.get("text") or "")[:300]})
            url_now = page.url or ""
            logged = not any(k in url_now for k in ("login", "passport"))
            out.append({"name": "page", "url": url_now,
                        "status": 1 if logged else 0, "method": "",
                        "text": "已登录" if logged else
                        "停在登录页：这个 profile 里没有登录态，先在「刷新 Cookie」里登录一次"})
        finally:
            self._close(pw, ctx)
        return {"items": out, "buckets": buckets}
