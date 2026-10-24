"""百家号驱动 —— 照搬 Wechatsync 的 BaijiahaoAdapter，并补上视频上传。

四步：登录检测 → 从编辑器页面抠 token → 传图/传视频换成自家地址 → 存草稿。
全程纯 HTTP + cookie，不需要浏览器，百家号没有反爬签名。

只做**图文**：动态（信息流短内容）平台没有可用的保存接口（见 style.FORMS 里
baijia.micro 的 publish=False），成稿只交付到 output_finish，人工发。

视频上传链路是从编辑器前端 bundle（bjh-mfe）里扒出来的，不是猜的：
1. POST /materialui/video/preuploadvideo  表单 app_id/media_type/mime/size/org_file_name
   → {error_code:20000, upload_key, mediaId}
2. POST /materialui/video/uploadvideo     分片 multipart（chunks 总数 / chunk 序号 / upload_key）
   → {error_code:20000}
3. POST /materialui/video/compuploadvideo 表单 upload_key/chunks/name/app_id/size/type/media_type
   → {error_code:0, mediaId}
4. POST /pcui/video/process               表单 mediaId，轮询到 data.status == PUBLISHED
   → data.base.src 是播放地址，data.img_frame[] 是平台截的封面帧
正文里的视频块（编辑器自己序列化出来的结构）：
<p data-bjh-box="video" data-bjh-video-media-id=.. data-bjh-video-status="published" \
contenteditable="false"><video class="vjs-matrix video-js" poster=.. controls controlsList="nodownload" \
width=400 height=225><source src=.. type="video/mp4"></video><span class="video-mask">…</span></p>
"""
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from .base import Article, AuthResult, PlatformDriver, SyncResult

log = logging.getLogger(__name__)

HOME = "https://baijiahao.baidu.com/"
APPINFO = "https://baijiahao.baidu.com/builder/app/appinfo"
EDITOR = "https://baijiahao.baidu.com/builder/rc/edit"
SAVE = "https://baijiahao.baidu.com/pcui/article/save?callback=bjhdraft"
UPLOAD = "https://baijiahao.baidu.com/pcui/picture/uploadproxy"
# 视频上传四步（见文件头注释）
VIDEO_PREUPLOAD = "https://baijiahao.baidu.com/materialui/video/preuploadvideo"
VIDEO_CHUNK = "https://baijiahao.baidu.com/materialui/video/uploadvideo"
VIDEO_COMPLETE = "https://baijiahao.baidu.com/materialui/video/compuploadvideo"
VIDEO_PROCESS = "https://baijiahao.baidu.com/pcui/video/process"
# 上传接口写死的业务参数，照搬原实现
APP_ID = "1589639493090963"
# 转码轮询：间隔 3s，最多等 240s（12s 短片正常 10~40s 就能好）
VIDEO_POLL_SECONDS = 3.0
VIDEO_POLL_TIMEOUT = 240.0
# 正文里的视频块（结构照抄编辑器序列化出来的 DOM）
VIDEO_BLOCK = (
    '<p data-bjh-box="video" data-bjh-id="video-box-{box}"'
    ' data-bjh-video-media-id="{media_id}" data-bjh-video-name="{name}"'
    ' data-bjh-video-status="published" data-bjh-video-poster="{poster}"'
    ' contenteditable="false">'
    '<video class="vjs-matrix video-js" poster="{poster}" controls="controls"'
    ' controlsList="nodownload" width="400" height="225">'
    '<source src="{src}" type="video/mp4"></video>'
    '<span class="video-mask"><span class="video-choose-cover">更换封面</span>'
    '<span class="remove-btn"></span></span></p>'
)
VIDEO_MARK = re.compile(r"<!--\s*video:(.*?)-->", re.S)


def _parse_body(text: str) -> Dict[str, Any]:
    """解析响应：JSONP（bjhdraft({...})）或纯 JSON。"""
    body = str(text or "").strip()
    if body.startswith("bjhdraft("):
        body = body[len("bjhdraft("):].rstrip(");")
    elif body.startswith("bjdraft("):
        body = body[len("bjdraft("):].rstrip(");")
    try:
        res = json.loads(body)
    except ValueError:
        return {"errmsg": f"返回体无法解析：{body[:120]}"}
    return res if isinstance(res, dict) else {"errmsg": "返回体不是对象"}

ORIGIN = "https://baijiahao.baidu.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _has_audio(path: "Path") -> Optional[bool]:
    """ffprobe 看有没有音轨：True/False 为确定结论，None 表示探测失败（无法判断）。

    探测失败不能「当有」——那会跳过补音轨，平台转码时才发现 NO_AUDIO，
    用户只看到一句「上传失败」，根本不知道是音轨问题。
    """
    from ..media.process import probe
    info = probe(path)
    if not info:
        return None
    return bool(info.get("has_audio"))


def _with_silent_audio(blob: bytes, filename: str) -> Tuple[bytes, str]:
    """给无音轨视频补一条静音轨（百家号转码缺音轨会返回 NO_AUDIO）。

    正常素材在 process_video 阶段已经保留原音轨（config.VIDEO_MUTE=False），
    这里只是兜底：老素材 / 外部片源 / 源片本身没有音轨的情况。
    补轨失败按原片上传（不阻断），但要留日志，免得又变成「无声上传失败」说不清原因。
    """
    import shutil
    import tempfile
    from pathlib import Path

    if not filename.lower().endswith((".mp4", ".mov", ".mkv", ".webm", ".flv")):
        return blob, filename
    tmp = Path(tempfile.mkdtemp(prefix="autonews-bjh-video-"))
    try:
        # ffmpeg 按内容识别封装格式，统一叫 in.mp4 是为了让补轨产出确定是 mp4
        src = tmp / "in.mp4"
        src.write_bytes(blob)
        if _has_audio(src) is True:
            return blob, filename
        from ..media.process import mux_silent_audio
        if not mux_silent_audio(src):
            log.warning("百家号：视频补静音轨失败，按原片上传（%s）", filename)
            return blob, filename
        name = filename if filename.lower().endswith(".mp4") else f"{Path(filename).stem}.mp4"
        return src.read_bytes(), name
    except Exception as exc:  # noqa: BLE001 - 补音轨失败就按原片上传
        log.warning("百家号：视频补静音轨异常（%s）：%s", filename, exc)
        return blob, filename
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _mime_of(name: str) -> str:
    low = str(name or "").lower()
    for ext, mime in ((".mp4", "video/mp4"), (".mov", "video/quicktime"),
                      (".mkv", "video/x-matroska"), (".webm", "video/webm"),
                      (".flv", "video/x-flv"), (".avi", "video/x-msvideo"),
                      (".png", "image/png"), (".webp", "image/webp"),
                      (".gif", "image/gif")):
        if low.endswith(ext):
            return mime
    return "image/jpeg"


class BaijiahaoDriver(PlatformDriver):
    id = "baijia"
    name = "百家号"
    homepage = HOME
    output_format = "html"
    # 这些域名本来就是百家号的图床，不用重复传
    skip_patterns = ("baijiahao.baidu.com", "bdstatic.com", "bcebos.com")

    def __init__(self, account: Dict[str, Any]) -> None:
        super().__init__(account)
        self._token = ""
        self._user: Dict[str, Any] = {}
        self._video_cache: Dict[str, Dict[str, Any]] = {}

    # ---------------------------------------------------------------- 会话
    def _sync_from_profile(self) -> str:
        """库里没有 cookie 时，从浏览器 profile 导一次。

        扫码登录成功后 cookie 已经落过库，这条只是兜底（老账号 / cookie 被清理过）。
        失败不抛异常：让 check_auth 给出「先去扫码登录」的明确提示，而不是抛栈。
        """
        if getattr(self, "_profile_synced", False):
            return ""
        self._profile_synced = True
        account_id = int(self.account.get("id") or 0)
        if not account_id:
            return ""
        try:
            from ..accounts import get_account, sync_cookie_from_profile
            sync_cookie_from_profile(account_id)
            fresh = get_account(account_id, include_cookie=True) or {}
        except Exception:  # noqa: BLE001 - 同步失败就当没有，交给上层提示
            return ""
        raw = str(fresh.get("cookie") or "").strip()
        if raw:
            self.account["cookie"] = raw
        return raw

    def _cookie_header(self) -> str:
        raw = str(self.account.get("cookie") or "").strip()
        if not raw:
            raw = self._sync_from_profile()
        if not raw:
            return ""
        try:
            items = json.loads(raw)
            if isinstance(items, list):  # playwright 抓下来的格式
                return "; ".join(
                    f"{c.get('name')}={c.get('value')}" for c in items
                    if c.get("name"))
        except ValueError:
            pass
        return raw  # 本身就是 "a=1; b=2" 也认

    def _client(self) -> httpx.Client:
        headers = {"User-Agent": UA, "Origin": ORIGIN, "Referer": HOME}
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        return httpx.Client(timeout=30, follow_redirects=True, headers=headers)

    # ---------------------------------------------------------------- 登录态
    def check_auth(self) -> AuthResult:
        if not self._cookie_header():
            return AuthResult(False, error="账号还没有 cookie，先去账号管理刷新")
        try:
            with self._client() as c:
                resp = c.get(APPINFO, params={"_": int(time.time() * 1000)})
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return AuthResult(False, error=f"登录检测失败：{exc}")
        user = (data.get("data") or {}).get("user") or {}
        if data.get("errmsg") == "success" and user:
            self._user = user
            return AuthResult(True, username=str(user.get("name") or ""),
                              user_id=str(user.get("userid") or ""),
                              avatar=str(user.get("avatar") or ""))
        return AuthResult(False, error=str(data.get("errmsg") or "未登录"))

    def _fetch_token(self) -> str:
        """百家号的写接口要一个 token，藏在编辑器页面的 JS 变量里。"""
        with self._client() as c:
            html = c.get(EDITOR).text
        for pattern in (r"window\.__BJH__INIT__AUTH__\s*=\s*['\"]([^'\"]+)['\"]",
                        r"__BJH__INIT__AUTH__\s*:\s*['\"]([^'\"]+)['\"]"):
            m = re.search(pattern, html)
            if m:
                return m.group(1)
        raise RuntimeError("登录失效，请重新登录百家号（没取到 token）")

    # ---------------------------------------------------------------- 传图
    def upload_image(self, src: str) -> str:
        blob, filename = self.read_image(src)
        ctype = _mime_of(filename)
        with self._client() as c:
            resp = c.post(
                UPLOAD,
                files={"media": (filename, blob, ctype)},
                data={"type": "image", "app_id": APP_ID, "is_waterlog": "1",
                      "save_material": "1", "no_compress": "0", "is_events": "",
                      "article_type": "news"},
            )
            res = resp.json()
        ret = res.get("ret") or {}
        if res.get("errmsg") != "success" or not ret.get("https_url"):
            raise RuntimeError(str(res.get("errmsg") or "图片上传失败"))
        return str(ret["https_url"])

    # ---------------------------------------------------------------- 传视频
    def upload_video(self, src: str) -> Dict[str, Any]:
        """把本地视频传到百家号视频库，返回 {media_id, src, poster}。

        四步全按编辑器前端自己的报文来（见文件头），任何一步不通就抛异常，
        由调用方决定「这条视频不带进草稿」而不是整篇失败。
        """
        cached = self._video_cache.get(src)
        if cached:
            return cached
        blob, filename = self.read_image(src)
        if not blob:
            raise RuntimeError(f"视频文件读不出来：{src}")
        mime = _mime_of(filename)
        if not mime.startswith("video/"):
            raise RuntimeError(f"不是视频文件：{filename}")
        # 百家号转码要求必须有音轨（NO_AUDIO）：没有音轨的先补一条静音轨兜底
        blob, filename = _with_silent_audio(blob, filename)
        mime = _mime_of(filename)
        size = len(blob)
        token = {"token": self._token}

        with self._client() as c:
            # 1) 预上传：拿 upload_key / mediaId
            pre = c.post(VIDEO_PREUPLOAD, headers=token,
                         data={"app_id": APP_ID, "media_type": "video", "mime": mime,
                               "size": str(size), "org_file_name": filename})
            try:
                pre_json = pre.json()
            except ValueError:
                pre_json = {}
            if int(pre_json.get("error_code") or 0) != 20000:
                raise RuntimeError("视频预上传失败：" + str(pre_json.get("error_msg")
                                                          or pre.text[:160]))
            upload_key = str(pre_json.get("upload_key") or "")
            media_id = str(pre_json.get("mediaId") or "")
            if not upload_key:
                raise RuntimeError("视频预上传没返回 upload_key")

            # 2) 分片上传：短片一个分片就够（chunks=1, chunk=0）
            up = c.post(
                VIDEO_CHUNK, headers=token,
                files={"file": (filename, blob, mime)},
                data={"timestamp": str(int(time.time() * 1000)),
                      "id": f"WU_FILE_{int(time.time() * 1000)}",
                      "name": filename, "type": mime,
                      "lastModifiedDate": time.strftime("%a %b %d %Y %H:%M:%S GMT%z"),
                      "size": str(size), "chunks": "1", "chunk": "0",
                      "upload_key": upload_key},
            )
            try:
                up_json = up.json()
            except ValueError:
                up_json = {}
            if int(up_json.get("error_code") or 0) != 20000:
                raise RuntimeError("视频分片上传失败：" + str(up_json.get("error_msg")
                                                          or up.text[:160]))

            # 3) 合并分片：拿 mediaId
            done = c.post(VIDEO_COMPLETE, headers=token,
                          data={"upload_key": upload_key, "chunks": "1", "name": filename,
                                "app_id": APP_ID, "size": str(size), "type": "video",
                                "media_type": "video"})
            try:
                done_json = done.json()
            except ValueError:
                done_json = {}
            if int(done_json.get("error_code") or 0) != 0:
                raise RuntimeError("视频合并失败：" + str(done_json.get("error_msg")
                                                       or done.text[:160]))
            media_id = str(done_json.get("mediaId") or media_id)
            if not media_id:
                bos = str(done_json.get("bos_url") or "")
                try:
                    media_id = str((json.loads(bos) or {}).get("mediaId") or "")
                except ValueError:
                    media_id = ""
            if not media_id:
                raise RuntimeError("视频上传后没拿到 mediaId")

            # 4) 等转码完成：要的是播放地址和封面帧
            data = self._poll_video(media_id, token)
        out = {
            "media_id": media_id,
            "src": str((data.get("base") or {}).get("src") or ""),
            "poster": (data.get("img_frame") or [""])[0],
            "name": filename,
        }
        if not out["src"]:
            raise RuntimeError("视频转码完成但没拿到播放地址")
        self._video_cache[src] = out
        return out

    def _poll_video(self, media_id: str, token: Dict[str, str]) -> Dict[str, Any]:
        """轮询 /pcui/video/process 直到 PUBLISHED，返回 data。"""
        deadline = time.time() + VIDEO_POLL_TIMEOUT
        last = ""
        while time.time() < deadline:
            with self._client() as c:
                resp = c.post(VIDEO_PROCESS, headers=token, data={"mediaId": media_id})
            try:
                res = resp.json()
            except ValueError:
                last = resp.text[:160]
                time.sleep(VIDEO_POLL_SECONDS)
                continue
            if int(res.get("errno") or 0) != 0:
                raise RuntimeError("视频转码查询失败：" + str(res.get("errmsg") or "未知错误"))
            data = res.get("data") or {}
            status = str(data.get("status") or "")
            if status == "PUBLISHED":
                return data
            if status in ("FAILED", "DISABLED", "BANNED"):
                raise RuntimeError(f"视频转码失败（{status}）")
            last = status or last
            time.sleep(VIDEO_POLL_SECONDS)
        raise RuntimeError(f"视频转码超时（等了 {int(VIDEO_POLL_TIMEOUT)} 秒，最后状态：{last or '未知'}）")

    @staticmethod
    def _video_block(info: Dict[str, Any], index: int) -> str:
        return VIDEO_BLOCK.format(
            box=index, media_id=info.get("media_id") or "",
            name=str(info.get("name") or "video.mp4").replace('"', "&quot;"),
            poster=str(info.get("poster") or "").replace('"', "&quot;"),
            src=str(info.get("src") or "").replace('"', "&quot;"))

    def _embed_videos(self, content: str, on_progress: Optional[Callable[[int, int], None]] = None
                      ) -> Tuple[str, int, List[str]]:
        """把正文里的视频占位符换成百家号的视频块。

        返回 (新正文, 成功带上的视频数, 失败原因列表)。失败的那条只去掉占位符，
        不影响整篇存草稿 —— 用户可以在百家号编辑器里手动插。
        """
        marks = list(VIDEO_MARK.finditer(content))
        if not marks:
            return content, 0, []
        out = content
        done = 0
        errors: List[str] = []
        for idx, m in enumerate(marks, 1):
            src = (m.group(1) or "").strip()
            if on_progress:
                on_progress(idx, len(marks))
            if not src:
                out = out.replace(m.group(0), "")
                continue
            try:
                info = self.upload_video(src)
                out = out.replace(m.group(0), self._video_block(info, idx))
                done += 1
            except Exception as exc:  # noqa: BLE001 - 单条视频失败不该毁整篇
                out = out.replace(m.group(0), "")
                errors.append(f"{src.rsplit('/', 1)[-1]}：{exc}")
        return out, done, errors

    # ---------------------------------------------------------------- 发布
    def publish(self, article: Article, draft_only: bool = True,
                on_progress: Optional[Callable[[int, int], None]] = None) -> SyncResult:
        # 动态（短内容）没有可用的保存接口：只交付成稿，人工发
        if str(getattr(article, "form", "article") or "article") == "micro":
            return self.fail(
                "百家号动态暂不做自动发布（平台没有可用的保存接口，抓包也被安全验证拦），"
                "成稿已交付到 output_finish，请到百家号后台手动发；图文会正常存草稿")
        try:
            auth = self.check_auth()
            if not auth.ok:
                return self.fail(auth.error or "请先登录百家号")
            self._token = self._fetch_token()

            # 图片：传不上去的直接剔除，别把本地路径留在草稿里（会显示成碎图）
            content, uploaded = self.process_images(article.html or "", on_progress,
                                                    drop_failed=True)
            # 视频：传上去的换成百家号视频块，传不上的只去掉占位符并记原因
            from .. import settings
            if settings.get_bool("publish.baijia_video", True):
                content, videos, video_errors = self._embed_videos(content, on_progress)
            else:
                video_count = len(VIDEO_MARK.findall(content))
                content = VIDEO_MARK.sub("", content)
                videos = 0
                video_errors = ([f"{video_count} 个视频未上传（设置里关闭了「百家号图文带视频」）"]
                                if video_count else [])
            if not content.strip():
                return self.fail("正文为空，没法发布")

            payload = {
                "title": article.title,
                "content": content,
                "feed_cat": "1",
                "len": str(len(content)),
                "activity_list": json.dumps([{"id": 408, "is_checked": 0}]),
                "source_reprinted_allow": "0",
                "original_status": "0",
                "original_handler_status": "1",
                "isBeautify": "false",
                "subtitle": "",
                "bjhtopic_id": "",
                "bjhtopic_info": "",
                "type": "news",
            }
            with self._client() as c:
                resp = c.post(SAVE, data=payload, headers={"token": self._token})
            res = _parse_body(resp.text)
            ret = res.get("ret") or {}
            if res.get("errmsg") != "success" or not ret.get("article_id"):
                return self.fail(str(res.get("errmsg") or "保存草稿失败"))

            post_id = str(ret["article_id"])
            note = f"已保存草稿，上传图片 {uploaded} 张"
            if videos:
                note += f"、视频 {videos} 个"
            if video_errors:
                note += "；视频没带上：" + "；".join(video_errors)
            return self.ok(
                post_id=post_id,
                post_url=f"https://baijiahao.baidu.com/builder/rc/edit?type=news&article_id={post_id}",
                draft_only=draft_only,
                message=note,
            )
        except Exception as exc:  # noqa: BLE001
            return self.fail(str(exc))
