"""SQLite 数据访问层。"""
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from .config import DB_PATH

_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    size INTEGER DEFAULT 0,
    status TEXT DEFAULT 'parsing',
    total INTEGER DEFAULT 0,
    inserted INTEGER DEFAULT 0,
    duplicated INTEGER DEFAULT 0,
    message TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER,
    title TEXT DEFAULT '',
    source TEXT DEFAULT '',
    source_norm TEXT DEFAULT '',      -- 来源归一化：toutiao / baijia / other（仅标记筛选）
    author TEXT DEFAULT '',
    orig_category TEXT DEFAULT '',
    category TEXT DEFAULT '',
    content_type TEXT DEFAULT '',
    read_count INTEGER DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    url TEXT DEFAULT '',
    publish_time TEXT DEFAULT '',
    publish_date TEXT DEFAULT '',
    fetch_status TEXT DEFAULT 'pending',
    fetch_error TEXT DEFAULT '',
    content_id INTEGER,
    fetched_at TEXT DEFAULT '',
    finish_status TEXT DEFAULT '',   -- 成稿交付：pending / running / success / failed
    finish_at TEXT DEFAULT '',
    finish_dir TEXT DEFAULT '',      -- 交付目录（相对 output_finish，多平台逗号分隔）
    sensitive_status TEXT DEFAULT '', -- 敏感词结论：pass / fixed / blocked（看板筛选用）
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url ON articles(url) WHERE url <> '';
CREATE INDEX IF NOT EXISTS idx_articles_date ON articles(publish_date);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(fetch_status);
CREATE INDEX IF NOT EXISTS idx_articles_batch ON articles(batch_id);

CREATE TABLE IF NOT EXISTS raw_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER,
    url TEXT DEFAULT '',
    title TEXT DEFAULT '',
    content_type TEXT DEFAULT '文章',
    summary TEXT DEFAULT '',
    text_len INTEGER DEFAULT 0,
    image_count INTEGER DEFAULT 0,
    video_count INTEGER DEFAULT 0,
    images TEXT DEFAULT '[]',
    videos TEXT DEFAULT '[]',
    path TEXT DEFAULT '',
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cache_type ON raw_cache(content_type);
CREATE INDEX IF NOT EXISTS idx_cache_article ON raw_cache(article_id);

CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER,
    kind TEXT DEFAULT 'image',        -- image / video / frame
    keyword TEXT DEFAULT '',
    provider TEXT DEFAULT '',         -- bing / pexels / bilibili ...
    platform TEXT DEFAULT '',         -- 素材来源平台
    src_url TEXT DEFAULT '',
    page_url TEXT DEFAULT '',
    path TEXT DEFAULT '',
    url TEXT DEFAULT '',              -- 可访问路径 /output/...
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    bytes INTEGER DEFAULT 0,
    duration REAL DEFAULT 0,
    match REAL DEFAULT 0,             -- 与原文标题的相关度 0~1
    clip_start REAL DEFAULT 0,        -- 视频截取起点（跳过片头 / 黑场）
    bands TEXT DEFAULT '',            -- 被裁掉的边缘文字条，逗号分隔：top/bottom/left/right
    status TEXT DEFAULT 'ready',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_article ON media_assets(article_id);
CREATE INDEX IF NOT EXISTS idx_media_kind ON media_assets(kind);

-- 大模型接入配置（页面「系统设置 → 大模型配置」维护）
CREATE TABLE IF NOT EXISTS llm_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'article',   -- article 文章改写 / video 视频生成
    provider TEXT DEFAULT 'custom',             -- deepseek / qwen / openai / claude / gemini / ollama / custom
    api_style TEXT DEFAULT 'openai',           -- 请求协议：openai / anthropic / gemini
    base_url TEXT DEFAULT '',
    api_key TEXT DEFAULT '',
    model TEXT DEFAULT '',
    temperature REAL DEFAULT 0.7,
    top_p REAL DEFAULT 1,
    max_tokens INTEGER DEFAULT 4096,
    timeout INTEGER DEFAULT 90,
    priority INTEGER DEFAULT 0,                -- 数值越大越优先
    enabled INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,     -- 该分类下的默认模型
    system_prompt TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_category ON llm_configs(category);

-- 运行时配置（浏览器路径 / ffmpeg 路径 / 抓取与改写参数）
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT '',
    category TEXT DEFAULT 'general',
    label TEXT DEFAULT '',
    hint TEXT DEFAULT '',
    input_type TEXT DEFAULT 'text',            -- text / textarea / number / select / secret
    options TEXT DEFAULT '[]',                 -- select 的候选项（JSON）
    prober TEXT DEFAULT '',                    -- file / dir / bin，页面「探测」按钮用
    is_secret INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_settings_cat ON app_settings(category);

-- 改写产物：标题改写 → 正文改写 → 自然化润色，三轮的中间稿都留着
CREATE TABLE IF NOT EXISTS article_rewrites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER,
    cache_id INTEGER,
    target_platform TEXT DEFAULT '',   -- 该版本适配的平台：toutiao / baijia
    content_form TEXT DEFAULT 'article',  -- 内容形态：article 图文 / micro 微头条
    variant_group TEXT DEFAULT '',     -- 同一原文的多平台版本归一组（= article_id:batch）
    origin_title TEXT DEFAULT '',
    origin_text TEXT DEFAULT '',
    titles TEXT DEFAULT '[]',         -- 标题候选 JSON：[{style, title}]
    used_title TEXT DEFAULT '',
    draft_text TEXT DEFAULT '',       -- 正文第 2 轮（深度改写）输出
    final_text TEXT DEFAULT '',       -- 正文第 3 轮（润色）输出，未润色时 = draft_text
    words INTEGER DEFAULT 0,
    stage TEXT DEFAULT '',            -- 失败时停在哪一步：title / body / polish / sensitive
    status TEXT DEFAULT 'pending',    -- pending / running / success / failed
    sensitive_status TEXT DEFAULT '', -- pass（一次过）/ fixed（修复后过）/ blocked（未过）/ waived（人工放行）
    sensitive_hits TEXT DEFAULT '[]', -- 命中明细 JSON：[{word,tier,file,field,count,snippets}]
    sensitive_rounds INTEGER DEFAULT 0,   -- 实际跑了几轮修复润色
    sensitive_checked_at TEXT DEFAULT '',
    sensitive_waived_at TEXT DEFAULT '',   -- 人工放行时间
    sensitive_waived_note TEXT DEFAULT '', -- 放行备注（谁批的、为什么）
    attempts INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    model TEXT DEFAULT '',
    elapsed REAL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 敏感词校验留痕：每轮「命中词 + 待修稿 + 修后稿」都存一条，便于回溯为什么改了这句
CREATE TABLE IF NOT EXISTS sensitive_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER,
    rewrite_id INTEGER,
    round INTEGER DEFAULT 0,          -- 0 = 润色后首检，1..N = 第 N 轮修复后
    platform TEXT DEFAULT '',
    content_form TEXT DEFAULT 'article',
    status TEXT DEFAULT '',           -- pass / fail
    hits TEXT DEFAULT '[]',           -- 本轮命中明细
    before_text TEXT DEFAULT '',      -- 交给模型前的稿件（round 0 为润色稿）
    after_text TEXT DEFAULT '',       -- 模型返回并清洗后的稿件
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sensitive_article ON sensitive_checks(article_id);
CREATE INDEX IF NOT EXISTS idx_sensitive_rewrite ON sensitive_checks(rewrite_id);

CREATE INDEX IF NOT EXISTS idx_rewrite_article ON article_rewrites(article_id);
CREATE INDEX IF NOT EXISTS idx_rewrite_cache ON article_rewrites(cache_id);

-- 排版产物：改写稿 + 素材插入方案，可手动调整后导出
CREATE TABLE IF NOT EXISTS article_layouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER,
    rewrite_id INTEGER,
    content_form TEXT DEFAULT 'article',  -- 内容形态：article 图文 / micro 微头条
    title TEXT DEFAULT '',
    body_md TEXT DEFAULT '',        -- 最终排版稿（含图片/视频占位）
    blocks TEXT DEFAULT '[]',       -- 正文结构：[{type:'h2'|'h3'|'p', text}]
    placements TEXT DEFAULT '[]',   -- 插入方案：[{id, kind, asset_id, after_block, reason, enabled}]
    assets TEXT DEFAULT '[]',       -- 参与排版的素材快照
    image_count INTEGER DEFAULT 0,
    video_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'draft',    -- draft / exported
    output_path TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_layout_article ON article_layouts(article_id);
CREATE INDEX IF NOT EXISTS idx_layout_rewrite ON article_layouts(rewrite_id);

-- 发布账号：按平台分类管理，每个账号绑定独立浏览器 profile 保存登录态
CREATE TABLE IF NOT EXISTS publish_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL DEFAULT 'custom',  -- toutiao / baijia / zhihu / gongzhonghao / ...
    name TEXT NOT NULL DEFAULT '',            -- 账号备注名
    login_user TEXT DEFAULT '',               -- 登录用户名 / 手机号
    home_url TEXT DEFAULT '',                 -- 平台后台首页，用于刷新 cookie
    profile_dir TEXT DEFAULT '',              -- 浏览器用户数据目录（登录态存在这里）
    executable_path TEXT DEFAULT '',          -- 浏览器可执行文件，留空用全局设置
    headless INTEGER NOT NULL DEFAULT 0,      -- 刷新 cookie 时是否无头
    proxy TEXT DEFAULT '',
    cookie TEXT DEFAULT '',                   -- 抓取到的 cookie（JSON 数组 / Netscape 串）
    cookie_updated_at TEXT DEFAULT '',
    cookie_expire_at TEXT DEFAULT '',         -- 最近一条会话 cookie 的过期时间
    auto_refresh INTEGER NOT NULL DEFAULT 0,
    refresh_hours INTEGER DEFAULT 12,         -- 自动续期间隔
    status TEXT DEFAULT 'unknown',            -- unknown / valid / expired / error
    last_checked_at TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    api_endpoints TEXT DEFAULT '',             -- 探测抓到的真实接口 {"upload":[],"publish":[],"user_info":[]}
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_platform ON publish_accounts(platform);

-- 分发任务：一篇排版稿 × 一个平台账号 = 一条
CREATE TABLE IF NOT EXISTS publish_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER,
    layout_id INTEGER,
    account_id INTEGER,
    platform TEXT DEFAULT '',
    content_form TEXT DEFAULT 'article',  -- 内容形态：article 图文 / micro 微头条
    title TEXT DEFAULT '',            -- 实际发布用的标题（可能与排版稿不同）
    draft_only INTEGER NOT NULL DEFAULT 1,
    status TEXT DEFAULT 'pending',    -- pending / running / success / failed
    post_id TEXT DEFAULT '',
    post_url TEXT DEFAULT '',
    image_count INTEGER DEFAULT 0,    -- 成功上传到平台的图片数
    video_skipped INTEGER DEFAULT 0,  -- 平台不支持、需手动上传的视频数
    error TEXT DEFAULT '',
    elapsed REAL DEFAULT 0,
    attempts INTEGER DEFAULT 0,        -- 已尝试次数（重试累加）
    max_attempts INTEGER DEFAULT 3,
    heartbeat_at TEXT DEFAULT '',      -- worker 心跳：用来回收进程重启留下的僵尸任务
    cancel_requested INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_publish_article ON publish_tasks(article_id);
CREATE INDEX IF NOT EXISTS idx_publish_account ON publish_tasks(account_id);
CREATE INDEX IF NOT EXISTS idx_publish_status ON publish_tasks(status);

-- 账号数据快照：每次采集把平台返回的原始 JSON 存一份，方便回溯
CREATE TABLE IF NOT EXISTS insight_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    platform TEXT DEFAULT '',
    kind TEXT DEFAULT '',            -- works / fans / income / overview
    payload TEXT DEFAULT '[]',       -- 采集到的原始 JSON（数组）
    captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_insight_snap ON insight_snapshots(account_id, kind);

-- 作品维度数据：同一作品多次采集覆盖更新
CREATE TABLE IF NOT EXISTS insight_works (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    platform TEXT DEFAULT '',
    item_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    status TEXT DEFAULT '',
    show_count INTEGER DEFAULT 0,
    read_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    publish_time TEXT DEFAULT '',
    captured_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_insight_work
    ON insight_works(account_id, item_id) WHERE item_id <> '';
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        # 部分文件系统（如 WSL 挂载的 /mnt/c、/mnt/e）不支持 WAL，失败时自动回退
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def write_conn() -> Iterator[sqlite3.Connection]:
    """写操作串行化，避免并发导入/抓取时的锁竞争。"""
    with _write_lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    with write_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
    for name in ("content_type", "updated_at", "fetched_at"):
        if name not in cols:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {name} TEXT DEFAULT ''")
    if "content_id" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN content_id INTEGER")
    # 导出目录与素材收集状态
    for name in ("output_dir", "media_status", "media_error", "media_at"):
        if name not in cols:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {name} TEXT DEFAULT ''")
    if "media_count" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN media_count INTEGER DEFAULT 0")
    # 改写状态（指向最新一条 article_rewrites）
    for name in ("rewrite_status", "rewrite_error", "rewrite_at"):
        if name not in cols:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {name} TEXT DEFAULT ''")
    if "rewrite_id" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN rewrite_id INTEGER")
    # 素材与原文标题的相关度
    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(media_assets)")}
    if "match" not in mcols:
        conn.execute("ALTER TABLE media_assets ADD COLUMN match REAL DEFAULT 0")
    for col, ddl in (("clip_start", "REAL DEFAULT 0"), ("bands", "TEXT DEFAULT ''"),
                     ("title", "TEXT DEFAULT ''"),   # 素材自身标题（图片站点名 / 视频标题）
                     ("name", "TEXT DEFAULT ''")):    # 落盘文件名
        if col not in mcols:
            conn.execute(f"ALTER TABLE media_assets ADD COLUMN {col} {ddl}")
    # 账号表：探测抓到的真实接口（老库补列）
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(publish_accounts)")}
    if "api_endpoints" not in acols:
        conn.execute("ALTER TABLE publish_accounts ADD COLUMN api_endpoints TEXT DEFAULT ''")
    # 来源归一化（仅标记筛选）：老库补列后按需回填
    if "source_norm" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN source_norm TEXT DEFAULT ''")
    # 成稿交付状态（output_finish 自动导出）
    for col, ddl in (("finish_status", "TEXT DEFAULT ''"),
                     ("finish_at", "TEXT DEFAULT ''"),
                     ("finish_dir", "TEXT DEFAULT ''"),
                     ("sensitive_status", "TEXT DEFAULT ''")):
        if col not in cols:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {ddl}")
    # 排版记录按平台分开存（头条版 / 百家版各自一份素材与位置）
    lcols = {r["name"] for r in conn.execute("PRAGMA table_info(article_layouts)")}
    for col, ddl in (("platform", "TEXT DEFAULT ''"),
                     ("content_form", "TEXT DEFAULT 'article'")):  # 头条图文 / 头条微头条
        if col not in lcols:
            conn.execute(f"ALTER TABLE article_layouts ADD COLUMN {col} {ddl}")
    # 多平台版本：一篇原文 × 一个目标平台 × 一种形态 = 一条改写记录
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(article_rewrites)")}
    for col, ddl in (("target_platform", "TEXT DEFAULT ''"),
                     ("content_form", "TEXT DEFAULT 'article'"),
                     ("variant_group", "TEXT DEFAULT ''"),
                     ("sensitive_status", "TEXT DEFAULT ''"),
                     ("sensitive_hits", "TEXT DEFAULT '[]'"),
                     ("sensitive_rounds", "INTEGER DEFAULT 0"),
                     ("sensitive_checked_at", "TEXT DEFAULT ''"),
                     ("sensitive_waived_at", "TEXT DEFAULT ''"),
                     ("sensitive_waived_note", "TEXT DEFAULT ''")):
        if col not in rcols:
            conn.execute(f"ALTER TABLE article_rewrites ADD COLUMN {col} {ddl}")
    # 发布任务也记形态：微头条与图文走不同入口
    tcols = {r["name"] for r in conn.execute("PRAGMA table_info(publish_tasks)")}
    if "content_form" not in tcols:
        conn.execute("ALTER TABLE publish_tasks ADD COLUMN content_form TEXT DEFAULT 'article'")
    # 发布任务：心跳 / 重试 / 取消（任务化后老库补列）
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(publish_tasks)")}
    for col, ddl in (("attempts", "INTEGER DEFAULT 0"),
                     ("max_attempts", "INTEGER DEFAULT 3"),
                     ("heartbeat_at", "TEXT DEFAULT ''"),
                     ("cancel_requested", "INTEGER DEFAULT 0")):
        if col not in pcols:
            conn.execute(f"ALTER TABLE publish_tasks ADD COLUMN {col} {ddl}")
    # 素材状态口径修正：按标题关键词检索不到视频（或一条素材都没命中）是正常结果，
    # 不能算「素材异常」。老库里的历史记录先按下面两条规则纠正一遍，避免继续堆在异常列。
    conn.execute(
        "UPDATE articles SET media_status='success'"
        " WHERE media_status='failed' AND COALESCE(media_count,0) > 0")
    conn.execute(
        "UPDATE articles SET media_status='empty',"
        " media_error='无匹配素材（按标题检索未命中，正常跳过）'"
        " WHERE media_status='failed' AND COALESCE(media_count,0)=0"
        " AND (media_error LIKE '%未采到视频%'"
        "      OR media_error LIKE '%未找到可用素材%'"
        "      OR media_error LIKE '%图片只取到 0 张%')")
    # 老文案里「未采到视频：……宁缺勿滥」整段是提示，不是错误，改写成正常提示，
    # 让流水线看板不再用红字显示（旧的失败行转成功后也会走到这里）。
    for row in conn.execute(
            "SELECT id, media_error FROM articles"
            " WHERE media_status='success' AND media_error LIKE '%未采到视频%'").fetchall():
        msg = row["media_error"] or ""
        _head, sep, tail = msg.partition("宁缺勿滥")
        rest = tail.lstrip("；; ") if sep else ""
        fixed = "提示：无视频素材（正常）" + (("；" + rest) if rest else "")
        conn.execute("UPDATE articles SET media_error=? WHERE id=?", (fixed[:400], row["id"]))
    # 没有合格改写稿却被记成「交付失败」的历史记录：那是素材采集阶段提前触发交付
    # 造成的假异常（正文必然为空），重置成未交付，别让它一直挂在流水线异常列。
    conn.execute(
        "UPDATE articles SET finish_status='', finish_at='', finish_dir=''"
        " WHERE finish_status='failed'"
        " AND NOT EXISTS (SELECT 1 FROM article_rewrites r"
        "                 WHERE r.article_id = articles.id AND r.status='success')")
