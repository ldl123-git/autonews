"""平台识别：判定原文归属平台，并在素材搜索时排除同名平台（跨平台取材）。"""
from typing import Optional
from urllib.parse import urlparse

# 域名关键字 -> 平台
PLATFORM_RULES = {
    "toutiao": "今日头条",
    "toutiaostatic": "今日头条",
    "ixigua": "西瓜视频",
    "douyin": "抖音",
    "iesdouyin": "抖音",
    "kuaishou": "快手",
    "baijiahao": "百家号",
    "bdstatic": "百度",
    "baidu": "百度",
    "sina": "新浪",
    "weibo": "微博",
    "sohu": "搜狐",
    "163": "网易",
    "netease": "网易",
    "qq": "腾讯",
    "qqnews": "腾讯新闻",
    "zhihu": "知乎",
    "xiaohongshu": "小红书",
    "xhs": "小红书",
    "bilibili": "哔哩哔哩",
    "biliimg": "哔哩哔哩",
    "thepaper": "澎湃新闻",
    "ifeng": "凤凰网",
    "cntv": "央视网",
    "cctv": "央视网",
    "chinanews": "中新网",
    "people": "人民网",
    "xinhua": "新华网",
    "huanqiu": "环球网",
    "stcn": "证券时报",
    "yicai": "第一财经",
    "36kr": "36氪",
    "jiemian": "界面新闻",
    "nbd": "每日经济新闻",
    "caixin": "财新",
    "qunar": "去哪儿",
    "meituan": "美团",
    "pexels": "Pexels",
    "unsplash": "Unsplash",
    "pixabay": "Pixabay",
}

# 文字来源字段 -> 平台（导入的表格里 source 常是中文名）
SOURCE_ALIAS = {
    "头条": "今日头条", "今日头条": "今日头条", "西瓜": "西瓜视频", "抖音": "抖音",
    "快手": "快手", "百家号": "百家号", "百度": "百度", "新浪": "新浪", "微博": "微博",
    "搜狐": "搜狐", "网易": "网易", "腾讯": "腾讯", "知乎": "知乎", "小红书": "小红书",
    "哔哩哔哩": "哔哩哔哩", "b站": "哔哩哔哩", "澎湃": "澎湃新闻", "凤凰": "凤凰网",
    "央视": "央视网", "中新": "中新网", "人民网": "人民网", "新华": "新华网",
}

# 优先取材的其它平台（原文来自 A，则优先从这些找）
PREFERRED_DOMAINS = [
    "douyin.com", "baijiahao.com", "kuaishou.com", "ixigua.com",
    "bilibili.com", "xiaohongshu.com", "zhihu.com", "weibo.com",
]


def host_of(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def platform_of_url(url: str) -> str:
    host = host_of(url)
    return platform_of_host(host)


def platform_of_host(host: str) -> str:
    host = (host or "").lower()
    if not host:
        return ""
    # 精确匹配优先：逐级比对域名标签
    labels = host.split(".")
    for i in range(len(labels) - 1):
        key = ".".join(labels[i:])
        for rule, name in PLATFORM_RULES.items():
            if key == rule or key.startswith(rule + "."):
                return name
    for rule, name in PLATFORM_RULES.items():
        if rule in host:
            return name
    return host


def detect(url: str = "", source: str = "") -> str:
    """综合 URL 与 source 字段判定原文平台（source 优先，表格里的来源往往是中文名）。"""
    src = (source or "").strip()
    if src in SOURCE_ALIAS:
        return SOURCE_ALIAS[src]
    for alias, name in SOURCE_ALIAS.items():
        if alias and alias in src:
            return name
    return platform_of_url(url)


def is_same(platform_a: str, platform_b: str) -> bool:
    if not platform_a or not platform_b:
        return False
    return platform_a.strip().lower() == platform_b.strip().lower()


def preferred_domains(exclude: Optional[str] = None) -> list:
    """除原平台外优先取材的域名列表。"""
    out = []
    for d in PREFERRED_DOMAINS:
        if exclude and is_same(platform_of_host(d), exclude):
            continue
        out.append(d)
    return out
