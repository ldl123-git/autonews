"""分发基座：平台驱动的接口与通用图片处理。

契约照着 Wechatsync 的适配器来（Article / AuthResult / SyncResult / PublishOptions），
这样以后要接它的其它平台时，照搬 TS 实现即可。
"""
import datetime as dt
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clear_profile_locks(profile: Path) -> None:
    """清掉 chromium 非正常退出留下的单例锁。

    这些残留会让下次启动报 "profile is already in use by another instance"，
    即使其实一个进程都没跑。
    """
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile / name).unlink()
        except OSError:
            pass


def in_use_error(exc: Exception) -> bool:
    """判断是不是「profile 被另一个 chromium 实例占着」这类错。"""
    msg = str(exc)
    return ("already in use" in msg or "existing browser session" in msg
            or "SingletonLock" in msg)


def has_running_browser(profile: Path) -> bool:
    """确认是否真有 chrome 实例占着这个 profile。

    有锁文件不等于有实例在跑（非正常退出会留锁），所以直接查进程命令行。
    """
    target = str(profile)
    try:
        entries = [p.name for p in Path("/proc").iterdir() if p.name.isdigit()]
    except OSError:
        return True       # 查不到进程列表就保守起见，不擅自清锁
    for pid in entries:
        try:
            args = (Path("/proc") / pid / "cmdline").read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        if "chrome" in args and target in args:
            return True
    return False


def normalize_path(raw: str) -> str:
    """把 Windows 盘符路径转成 WSL 能用的路径。

    在 WSL 里 Path("E:\\\\autonews\\\\x") 会被当成文件名而不是绝对路径，
    目录会被建到当前工作目录下，登录态就白存了。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if m and Path("/mnt").exists():
        return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return str(Path(s).expanduser())


class Article:
    """待发布文章：标题 + 正文 HTML + 可选封面/标签。

    form 是内容形态：article（图文文章）/ micro（微头条，无标题、只存草稿）。
    驱动按 form 选择不同的发布入口，拿不到形态时按图文处理。
    """

    def __init__(self, title: str, html: str = "", markdown: str = "",
                 cover: str = "", tags: Optional[List[str]] = None,
                 summary: str = "", form: str = "article") -> None:
        self.title = title
        self.html = html
        self.markdown = markdown
        self.cover = cover
        self.tags = tags or []
        self.summary = summary
        self.form = form or "article"


class AuthResult:
    def __init__(self, ok: bool, username: str = "", user_id: str = "",
                 avatar: str = "", error: str = "") -> None:
        self.ok = ok
        self.username = username
        self.user_id = user_id
        self.avatar = avatar
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        return {"isAuthenticated": self.ok, "username": self.username,
                "userId": self.user_id, "avatar": self.avatar, "error": self.error}


class SyncResult:
    def __init__(self, platform: str, success: bool, post_id: str = "",
                 post_url: str = "", draft_only: bool = True, error: str = "",
                 message: str = "") -> None:
        self.platform = platform
        self.success = success
        self.post_id = post_id
        self.post_url = post_url
        self.draft_only = draft_only
        self.error = error
        self.message = message

    def to_dict(self) -> Dict[str, Any]:
        return {"platform": self.platform, "success": self.success,
                "postId": self.post_id, "postUrl": self.post_url,
                "draftOnly": self.draft_only, "error": self.error,
                "message": self.message}


class PlatformDriver:
    """平台驱动基类。子类只需实现三件事：登录检测、传图、发布。"""

    #: 平台标识，与 publish_accounts.platform 对应
    id: str = ""
    name: str = ""
    homepage: str = ""
    #: 该平台要 HTML 还是 Markdown
    output_format: str = "html"
    #: 平台自己的图片域名，命中就不重复上传
    skip_patterns: Tuple[str, ...] = ()
    #: 是否需要浏览器（有反爬签名的平台走这条）
    needs_browser: bool = False

    def __init__(self, account: Dict[str, Any]) -> None:
        self.account = account or {}

    # ---------------------------------------------------------------- 子类实现
    def check_auth(self) -> AuthResult:
        raise NotImplementedError

    def upload_image(self, src: str) -> str:
        """上传一张图，返回平台可访问的 URL。src 可以是本地路径或 http 链接。"""
        raise NotImplementedError

    def publish(self, article: Article, draft_only: bool = True,
                on_progress: Optional[Callable[[int, int], None]] = None) -> SyncResult:
        raise NotImplementedError

    # ---------------------------------------------------------------- 通用能力
    def ok(self, **kw: Any) -> SyncResult:
        return SyncResult(self.id, True, **kw)

    def fail(self, error: str, **kw: Any) -> SyncResult:
        return SyncResult(self.id, False, error=str(error)[:500], **kw)

    def upload_images(self, srcs: List[str],
                      on_progress: Optional[Callable[[int, int], None]] = None
                      ) -> List[str]:
        """批量传图，返回与 srcs 等长的地址列表（失败位给空串，由调用方回退内联）。

        默认实现就是逐个调 upload_image。需要复用浏览器会话的驱动（头条）要覆盖它：
        那边每调一次 upload_image 都要冷启动一次浏览器，复制一篇文章能等十几秒。
        """
        out: List[str] = []
        items = [str(s or "") for s in (srcs or [])]
        total = len(items)
        for i, src in enumerate(items, 1):
            if on_progress:
                on_progress(i, total)
            try:
                out.append(self.upload_image(src) or "")
            except Exception:  # noqa: BLE001 - 单张失败交给上层回退内联
                out.append("")
        return out

    def process_images(self, content: str,
                       on_progress: Optional[Callable[[int, int], None]] = None,
                       drop_failed: bool = False) -> Tuple[str, int]:
        """把正文里的图片全部换成平台自己的 URL。

        照搬 CodeAdapter.processImages：提取 <img src> 与 ![](url)，
        跳过平台自有域名，逐个上传并原地替换；单张失败不中断。
        返回 (新正文, 成功替换的图片数)。

        drop_failed=True 时，传不上去的图直接删掉而不是保留原链——
        头条这类平台会校验正文里的图片必须是自家图床，留一张外链整篇都会被拒。
        """
        matches: List[Tuple[str, str, str, bool]] = []  # (整段, src, alt, 是否 markdown)
        for m in re.finditer(r"<img[^>]+src=\"([^\"]+)\"[^>]*>", content, re.I):
            matches.append((m.group(0), m.group(1), "", False))
        for m in re.finditer(r"!\[([^\]]*)\]\(([^)\s]+)\)", content):
            matches.append((m.group(0), m.group(2), m.group(1), True))
        if not matches:
            return content, 0

        result = content
        cache: Dict[str, str] = {}
        done = 0
        total = len(matches)
        for idx, (full, src, alt, is_md) in enumerate(matches, 1):
            if not src:
                continue
            if not src.startswith("data:") and any(p in src for p in self.skip_patterns):
                continue
            if on_progress:
                on_progress(idx, total)
            try:
                url = cache.get(src)
                if not url:
                    url = self.upload_image(src)
                    cache[src] = url
                if not url:
                    raise RuntimeError("上传返回空地址")
                replacement = (f'<img src="{url}" alt="{alt}" />' if not is_md
                               else f"![{alt}]({url})")
                result = result.replace(full, replacement)
                done += 1
            except Exception:  # noqa: BLE001 - 一张图失败不该毁掉整篇
                if drop_failed:
                    result = result.replace(full, "")
                continue
        return result, done

    @staticmethod
    def read_image(src: str) -> Tuple[bytes, str]:
        """把 src 读成 (字节, 文件名)。本地路径直接读，http 链接下载。"""
        if src.startswith("data:"):
            import base64
            head, _, payload = src.partition(",")
            ext = "png" if "png" in head else "jpeg"
            return base64.b64decode(payload), f"image.{ext}"
        if src.startswith(("http://", "https://")):
            import httpx
            resp = httpx.get(src, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            name = src.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "image.jpg"
            return resp.content, name
        p = Path(src)
        return p.read_bytes(), p.name or "image.jpg"


# ---------------------------------------------------------------- Markdown → HTML
def md_to_html(md: str, keep_video: bool = False) -> str:
    """排版稿（Markdown）转成平台要的 HTML。

    只处理排版稿会用到的语法：标题、段落、图片、图注、加粗、引用、video 标签。

    keep_video=True 时，`<video src="..">` 行不会丢掉，而是留一个
    `<!--video:src-->` 占位符，交给驱动换成平台自己的视频块（百家号走这条）；
    默认 False：视频由平台单独上传，正文里不落。
    """
    def esc(s: str) -> str:
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

    out: List[str] = []
    para: List[str] = []

    def flush() -> None:
        if para:
            out.append("<p>" + "<br/>".join(para) + "</p>")
            para.clear()

    for raw in str(md or "").splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            level = min(6, len(m.group(1)))
            out.append(f"<h{level}>{esc(m.group(2))}</h{level}>")
            continue
        if re.match(r"^<video\b", line, re.I):
            flush()
            m_src = re.search(r'src="([^"]+)"', line)
            if keep_video and m_src:
                out.append(f"<!--video:{m_src.group(1)}-->")
            continue  # 视频要平台单独上传，正文里不直接落 <video>
        if line.startswith("> 视频"):
            flush()
            continue  # 排版稿里的视频说明行（带本地路径），不能贴进平台正文
        if line.startswith(">"):
            flush()
            out.append(f"<blockquote>{esc(line.lstrip('> '))}</blockquote>")
            continue
        mi = re.match(r"^!\[([^\]]*)\]\(([^)\s]+)\)$", line)
        if mi:
            flush()
            out.append(f'<img src="{mi.group(2)}" alt="{esc(mi.group(1))}" />')
            continue
        mi = re.match(r"^\*(.+)\*$", line)
        if mi:
            flush()
            out.append(f'<p class="caption">{esc(mi.group(1))}</p>')
            continue
        para.append(esc(line))
    flush()
    return "\n".join(out)


def count_videos(md: str) -> int:
    """排版稿里视频的数量（平台不支持直传时用来提示手动上传）。"""
    return len(re.findall(r"^<video\b", str(md or ""), re.M))
