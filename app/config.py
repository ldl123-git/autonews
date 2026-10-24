"""全局配置与目录定义。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
CACHE_DIR = DATA_DIR / "cache"
WEB_DIR = BASE_DIR / "web"
DB_PATH = DATA_DIR / "app.db"

# 采集结果导出目录：output/<标题>/
OUTPUT_DIR = BASE_DIR / "output"

# 扫码登录时截下来的二维码图片（前端轮询展示，成功后自动覆盖）
QR_DIR = DATA_DIR / "qrcode"

# 成稿交付目录：output_finish/<平台>/<标题>/（平台：tt=头条 / bjh=百家）
FINISH_DIR = BASE_DIR / "output_finish"

# 敏感词库目录（Sensitive-lexicon 仓库，词库在 <目录>/Vocabulary/*.txt）
# 设置页「敏感词校验 → 词库目录」可覆盖；留空用这个默认位置
SENSITIVE_DIR = BASE_DIR / "app" / "project" / "Sensitive-lexicon-main"
# 数据目录下的白名单 / 自定义词条（设置页里维护，这里只留位置便于人工查看）
SENSITIVE_DATA_DIR = DATA_DIR / "sensitive"

# 抓取并发与超时
FETCH_CONCURRENCY = 8
FETCH_TIMEOUT = 20.0
FETCH_RETRY = 1

# 导入时每批提交的行数
IMPORT_COMMIT_SIZE = 500

# 抓取时是否收集图片 / 视频地址（False = 只抓正文，不存任何媒体地址）
COLLECT_MEDIA = False
# 是否清洗正文（去掉站点推广、编辑署名、版权声明、APP 导流等噪声）
CLEAN_TEXT = True

# 抓取成功后是否自动导出到 output/<标题>/
AUTO_EXPORT = True

# ---------------------------------------------------------------- 素材收集
MEDIA_ENABLED = True
MEDIA_KEYWORD_COUNT = 5      # 从原文提取的关键词数量
MEDIA_IMAGE_COUNT = 10       # 图片素材目标数量：排版按文章长短取用，多采候选供人工审核挑选
MEDIA_IMAGE_MIN = 6          # 图片素材保底数量：低于此值继续降门槛补采（素材要人工筛，宁可多给）
MEDIA_VIDEO_COUNT = 1        # 视频素材数量
MEDIA_CONCURRENCY = 2        # 同时处理的文章数（素材下载/加工较重）
MEDIA_TIMEOUT = 30.0
MEDIA_MATCH_MIN = 0.15       # 素材与原文标题的相关度下限（素材充足时才按此过滤）
MEDIA_IMG_MATCH_MIN = 0.30   # 图片入库硬门槛：低于此直接丢弃（原文自带配图除外），宁缺勿滥
MEDIA_IMG_MATCH_MIN_SOFT = 0.12  # 保底补采门槛：凑不够保底数时才降到这一档，弱相关也留作候选
MEDIA_NEWS_MATCH_MIN = 0.25  # 报道页标题与原文标题的相关度门槛（低于此不视为同源报道）
MEDIA_USE_ORIGIN_IMAGES = True   # 是否把原文自带配图作为素材（匹配度最高）
MEDIA_MIN_IMAGE = (480, 270)     # 素材最小原始尺寸，过滤图标 / 缩略图
MEDIA_MAX_BYTES = 25 * 1024 * 1024   # 单个素材下载上限
# 检索词分层：标题层 = 完整标题（不分词），关键词层 = 正文关键词兜底
MEDIA_TITLE_QUERY_MAX = 60       # 完整标题作为检索词时的最大字数
MEDIA_KEYWORD_FALLBACK = True    # 标题层取不满图片时才用关键词层兜底（图片）
MEDIA_VIDEO_TITLE_ONLY = True    # 视频只用完整标题检索：关键词搜到的视频基本跑题，宁缺勿滥
MEDIA_VIDEO_MATCH_MIN = 0.18     # 视频相关度门槛：低于此不采（事件专名命中率为主，见 search.video_match）
MEDIA_VIDEO_MIN_HITS = 2         # 还要至少命中标题里 2 个实词才采（或一个事件专名），防止同主体不同事件
MEDIA_VIDEO_MIN_TAIL_LCS = 5     # 或「标题后半段」与视频标题连续重合 ≥5 字（撞的是事件而非同地名）
MEDIA_VIDEO_TITLE_SKIP = 8       # 判定后半段时跳过的开头字数（主语通常在前 8 字内）
MEDIA_USE_NEWS_VIDEOS = True     # 先取同源报道页里嵌的新闻视频（媒体原片，最贴题）
# 素材去重：不同 URL 常常是同一张图，按内容指纹（dHash 汉明距离）剔除重复
MEDIA_DEDUP = True
MEDIA_DEDUP_MAX_DIST = 6         # 汉明距离 ≤ 该值视为同一张图（0~64，越大越严格）

# 视频抽帧也当图片用：视频截出来的画面常常比搜索引擎搜到的图更贴题
MEDIA_FRAME_AS_IMAGE = True      # 抽帧并入排版图片池（关掉则只在图片不够时兜底）
MEDIA_FRAME_WEIGHT = 0.9         # 相关度折算系数：同分时排在同相关度的正式图片后面
MEDIA_FRAME_PER_VIDEO = 2        # 同一视频最多取几张帧进自动排版（避免同场景重复）
MEDIA_FRAME_SLOTS = 1            # 自动排版至少留几张名额给抽帧（0 = 纯按相关度，不保证用上）

# 图片二次加工
IMG_ASPECT = 16 / 9          # 目标宽高比（先按此比例居中裁剪）
IMG_TARGET = (1280, 720)     # 目标尺寸（等比缩放，不拉伸）
IMG_FORMAT = "JPEG"          # 输出格式：JPEG / WEBP / PNG
IMG_QUALITY = 82             # 起始压缩质量
IMG_MAX_BYTES = 600 * 1024   # 单张体积上限，超了自动降质量
IMG_STRIP_META = True        # 隐私处理：清除 EXIF / GPS / 拍摄信息
IMG_WATERMARK = ""           # 保持为空：产出图片上不叠加任何文字（含水印 / 署名）
IMG_TRIM_BORDER = 0.0        # 四周裁边比例（0.04 = 各边裁掉 4%，可去掉贴边的水印）
IMG_DEWATERMARK = True       # 检测并裁掉边缘文字条（站名水印 / 台标 / 字幕）
IMG_REJECT_TEXT_PLATE = True # 拒收「整张都是字」的图（警方通报截图 / 聊天记录 / 公告）
IMG_REJECT_LOGO = True       # 拒收台标 / 媒体 logo / 栏目横幅（CBH 中国商报这类）
IMG_FRAME_TARGET = (720, 405)  # 视频抽帧输出尺寸：抽帧会当配图用，对齐成片宽度上限（VIDEO_MAX_WIDTH）

# 视频二次加工
VIDEO_CLIP_SECONDS = 12      # 截取时长（10~15s）
VIDEO_CLIP_START = 2.0       # 起始秒（只跳过片头 / 黑场，保证内容与标题一致）
VIDEO_CLIP_MIDRATIO = 0.0    # >0 时取景中段（如 0.35）；默认 0 = 从头截取，避免跑题
VIDEO_MAX_WIDTH = 720
VIDEO_CRF = 26               # 越大压得越狠
VIDEO_FPS = 24
VIDEO_FRAMES = 6             # 抽帧张数（均匀抽取）
# 音轨：默认保留原声。去音轨（True）会让百家号等平台转码直接报 NO_AUDIO 上传失败，
# 交付目录里的成片也没声音；确需静音（例如自有片源带版权音乐）再改 True。
VIDEO_MUTE = False
VIDEO_AUDIO_BITRATE = 96     # 成片音轨码率（kbps）：语音类新闻 96k 足够
VIDEO_AUDIO_RATE = 44100     # 成片音轨采样率：平台转码普遍只吃 44.1k 立体声
VIDEO_SILENT_AUDIO = True    # 源片本身没音轨时补一条静音轨，保证平台能过转码
# 在线视频源普遍自带台标 / 站名水印，硬裁会破坏构图并因二次编码变糊，
# 因此默认关闭；确需处理自有片源时再改 True（见 process._video_crop_expr）
VIDEO_DEWATERMARK = False
VIDEO_FRAME_PROCESS = True   # 抽帧按图片标准再加工（统一尺寸 / 去 EXIF）
VIDEO_FRAME_DEWATERMARK = False  # 抽帧保持画面完整，不裁边缘字样（与视频一致）

# 素材源优先级（前置成功即停止）；可选 pexels / unsplash（需 API Key）
# 图片搜索引擎：参照 icrawler 的 BaiduImageCrawler / BingImageCrawler，无需 Key
IMAGE_PROVIDERS = ["baidu", "bing"]
# 通用图库兜底（需 API Key，取不到就是空）
STOCK_PROVIDERS = ["pexels", "unsplash"]
# 搜索引擎过滤条件（见 app/media/engines.py，对齐 icrawler 的 filter.py）
IMAGE_FILTERS = {"size": "large", "type": "photo", "layout": "wide"}
VIDEO_PROVIDERS = ["bilibili", "pexels"]
# 同源报道检索引擎（按顺序取，先出结果就够用）
# Bing 网页搜索对中文长标题已基本失效（只按首字召回百科），默认走百度新闻 + 360
NEWS_SEARCH_PROVIDERS = ["baidu", "so360"]
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

for _d in (DATA_DIR, UPLOAD_DIR, CACHE_DIR, OUTPUT_DIR, QR_DIR, FINISH_DIR, SENSITIVE_DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 成稿交付规则：素材按文章长短取用（见 layout.plan_counts），这里只是硬上限
FINISH_IMG_MIN = 1       # 至少 1 张图（取不到时不强求，导出会在 meta 里注明）
FINISH_IMG_MAX = 6       # 图片硬上限
FINISH_VIDEO_MAX = 1     # 最多 1 个视频（视频只做配片，不占图片名额）
FINISH_COPY_ASSETS = True  # 素材拷贝进交付目录（False = 只写原路径，不可整体打包）
FINISH_COPY_ALL = True   # 采集到的素材全部拷进交付目录，供人工审核时增删换

# ---------------------------------------------------------------- 一键复制（手动发布）
# 排版页「一键复制」把成稿做成富文本，直接粘进平台编辑器；图片有两种带法：
# inline = 压成 data:base64 内联（不触网，粘贴后由编辑器自己上传）；
# upload = 先传到平台图床再复制（最稳，需要账号登录态）；
# text   = 只给纯文本，图片位置留一行占位。
COPY_MODE = "inline"
COPY_MAX_WIDTH = 1080        # 内联图片最大宽度（太大粘贴慢，太小平台嫌糊）
COPY_JPEG_QUALITY = 82       # 内联图片起始质量
COPY_MAX_IMAGE_KB = 600      # 单张内联图体积上限，超了自动降质
COPY_MAX_TOTAL_MB = 8        # 整篇内联图总量上限，超了继续降质并提示
COPY_UPLOAD_LIMIT = 10       # 走平台图床时最多预传几张（其余自动改内联）
