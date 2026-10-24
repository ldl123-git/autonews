"""平台风格规则：每个平台版本走什么风格、产出哪几个版本。

版本数量与原文来源无关：正文是改写过的，两个平台都可以发。
（早期按「来源是头条只出百家版、来源是百家只出头条款」错开，这层限制已去掉——
改写后本来就是新稿，不存在必须避让来源平台的问题。）

来源列仍然会归一成 toutiao / baijia / other（normalize_source），
但只用于导入时打标记、列表筛选与统计，不再参与产出与发布决策。

平台范围按用户决策保持「头条 + 百家」两个驱动，不再往下扩。

在此之上再加一层「内容形态 form」：头条分图文与微头条，百家分图文与动态，
四个入口共用同一套判定——原文不到 400 字走短稿（头条微头条 / 百家号动态，
无标题、篇幅短），达到 400 字才走图文（需要标题）。形态由原文正文字数决定，
见 form_for()。

因此一篇原文只会产出「两版短稿」或「两版图文」其中一组，另一组形态留空：
短文不改写图文，长文不改写短稿（少一半改写量，排版也只对产出的那组有内容）。
"""
import re
from typing import Any, Dict, List

from . import settings

#: 交付目录用的短名：output_finish/tt/、output_finish/bjh/
PLATFORMS: Dict[str, Dict[str, str]] = {
    "toutiao": {
        "key": "toutiao",
        "dir": "tt",
        "title": "今日头条",
        "account_platform": "toutiao",
        "style": "头条风格",
    },
    "baijia": {
        "key": "baijia",
        "dir": "bjh",
        "title": "百家号",
        "account_platform": "baijia",
        "style": "百家号风格",
    },
}

#: 内容形态：key → {标题、交付目录、是否需要标题、风格说明、能否自动发布}
#: dir 只对「多形态的平台」细分：图文沿用老目录（tt / bjh），短稿单独开目录
#: （头条微头条 tt-wtt、百家号动态 bjh-dt）。
#: publish=False 表示网页端没有对应的自动发布入口，只能人工在后台发（仍是交付成稿）。
FORMS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "toutiao": {
        "article": {"key": "article", "title": "图文", "short": "图文", "dir": "tt",
                    "need_title": True, "style": "头条风格", "publish": True},
        "micro": {"key": "micro", "title": "微头条", "short": "微头条", "dir": "tt-wtt",
                  "need_title": False, "style": "头条微头条风格", "publish": True},
    },
    "baijia": {
        "article": {"key": "article", "title": "图文", "short": "图文", "dir": "bjh",
                    "need_title": True, "style": "百家号风格", "publish": True},
        # 百家号「动态」= 信息流短内容，与头条微头条同级：无标题、篇幅短
        # publish=False：平台没有可用的保存接口（抓包被安全验证拦），只交付成稿人工发
        "micro": {"key": "micro", "title": "动态", "short": "动态", "dir": "bjh-dt",
                  "need_title": False, "style": "百家号动态风格", "publish": False},
    },
}
DEFAULT_FORM = "article"
#: 提示词阶段：标题 / 正文 / 润色 / 敏感词修复（微头条没有标题阶段）
PROMPT_STAGES: tuple = ("title", "body", "polish", "fix")

DEFAULT_TT_ALIASES = ("头条", "今日头条", "头条号", "toutiao", "tt.")
DEFAULT_BJH_ALIASES = ("百家", "百家号", "baijia", "bjh", "百度", "baidu")

_STYLE_BRIEF = {
    "toutiao": (
        "标题 20~28 字；口语化、悬念前置，开头直接抛冲突或结果；"
        "段落短（2~3 句一段），多用具体数字与对比，少用书面套话。"
        "叙事角度：从最有冲突感的现场细节切入，先给结果再补背景，读者滑到第一屏就被抓住。"
    ),
    "baijia": (
        "标题 25~35 字；偏陈述、信息完整，利于搜索引擎收录；"
        "正文书面化、信息密度高，每 300 字左右用一个小标题分节。"
        "叙事角度：按事件脉络完整展开（起因—经过—结果—影响），先交代背景与通报信息，再给结论。"
    ),
}

#: 短稿（头条微头条 / 百家号动态）：无标题、短、口语化。
#: 字数目标控制在阈值以内，避免“短文写成大文章”。两个平台各一套口吻：
#: 标题都省略，区别在信息密度与语气（微头条更像口播，动态更像短资讯）。
_MICRO_BRIEF: Dict[str, str] = {
    "toutiao": (
        "微头条没有标题、也没有小标题：第一句直接抛事件结果或冲突，别写导语；"
        "全文 150~300 字，分 2~4 个短段，一段 1~2 句；口播式表达，少用书面套话；"
        "结尾可以一句提醒或提问，但不得编造事实、不得自行添加话题标签（#话题# 由发布时决定）。"
    ),
    "baijia": (
        "百家号动态没有标题、也没有小标题：第一句直接交代「谁 + 做了什么 + 结果」，"
        "不要“据悉 / 近日 / 随着”这类起手式；"
        "全文 120~260 字，分 2~4 个短段，一段 1~2 句，读一段就得到一个信息点；"
        "信息流资讯口吻，客观克制，不用感叹号堆情绪，不补原文没有的背景与猜测，"
        "不得自行添加话题标签。"
    ),
}
_MICRO_BRIEF_DEFAULT = (
    "短稿没有标题、也没有小标题：第一句直接交代事件结果，全文 120~300 字，"
    "分 2~4 个短段，口语化、客观克制，不补原文没有的信息，不添加话题标签。"
)

#: 注入到改写提示词末尾的一行「形态说明」（微头条 / 动态各一句）
_FORM_HINT: Dict[tuple, str] = {
    ("toutiao", "micro"): "微头条：没有标题、没有小标题，150~300 字，直接给正文。",
    ("baijia", "micro"): "百家号动态：没有标题、没有小标题，120~260 字，直接给正文。",
}
_FORM_HINT_DEFAULT = "短稿：没有标题、没有小标题，150~300 字，直接给正文。"

#: 两版差异化的「结构差异」要求：两版必须能被一眼看出不是同一篇稿子
DIFF_REQUIREMENT = (
    "【差异化要求】同一事件会分别产出头条版与百家版，本版必须与另一平台版本明显不同："
    "不得复用对方的标题、开头句、段落顺序与小标题；换一个叙事切入点，"
    "句式与段落组织也要有区别，严禁只做同义词替换。"
)


def diff_requirement() -> str:
    return DIFF_REQUIREMENT


def _aliases(key: str, default: tuple) -> tuple:
    """从配置读别名表（逗号分隔）；没配就用内置默认。"""
    raw = str(settings.get_setting(key) or "").strip()
    if not raw:
        return default
    items = tuple(x.strip().lower() for x in raw.replace("，", ",").split(",") if x.strip())
    return items or default


def normalize_source(source: Any) -> str:
    """把表格里的「来源」列归一成 toutiao / baijia / other。

    先判百家：`baijiahao.baidu.com` 这类值同时含 baidu 与 baijia，
    先匹配头条会把来源标记打错。
    """
    s = str(source or "").strip().lower()
    if not s:
        return "other"
    if any(k in s for k in _aliases("style.bjh_aliases", DEFAULT_BJH_ALIASES)):
        return "baijia"
    if any(k in s for k in _aliases("style.tt_aliases", DEFAULT_TT_ALIASES)):
        return "toutiao"
    return "other"


def targets_for(_source: Any = None) -> List[str]:
    """这条原文要产出哪几个平台版本：固定两个平台都出。

    source 参数保留只为兼容旧调用，不再参与判断——改写后的文本两个平台都能发，
    不按原文来源做限制。
    """
    return ["toutiao", "baijia"]


def count_chars(text: Any) -> int:
    """正文字数：去掉所有空白字符后的长度（形态判定的统一口径）。"""
    return len(re.sub(r"\s", "", str(text or "")))


def forms_of(platform: str) -> List[str]:
    """该平台支持的内容形态；没注册的平台退回图文。"""
    return list((FORMS.get(str(platform or "")) or {}).keys()) or [DEFAULT_FORM]


def form_meta(platform: str, form: str = "") -> Dict[str, Any]:
    """取某个形态的元数据；form 为空或未注册时退回 article。"""
    items = FORMS.get(str(platform or "")) or {}
    key = str(form or DEFAULT_FORM)
    return dict(items.get(key) or items.get(DEFAULT_FORM) or {"key": DEFAULT_FORM})


def form_key(platform: str, form: str = "") -> str:
    return str(form_meta(platform, form).get("key") or DEFAULT_FORM)


def short_limit() -> int:
    """短文阈值：原文去掉空白后不到这个字数，就只产出短稿（微头条 / 百家动态）。"""
    return max(1, settings.get_int("rewrite.micro_max_chars", 400))


def form_for(platform: str, chars: int = 0) -> str:
    """按原文正文字数决定形态（两个平台同一口径）。

    * 原文 < rewrite.micro_max_chars（默认 400 字）→ 短稿：头条微头条 / 百家号动态；
    * 原文 ≥ 该字数 → 图文：头条图文 / 百家号图文。
    没有 micro 形态的平台（以后可能新增的）永远走图文。
    """
    forms = forms_of(platform)
    if "micro" not in forms:
        return forms[0]
    return "micro" if int(chars or 0) < short_limit() else "article"


def need_title(platform: str, form: str = "") -> bool:
    """这个形态的成稿是否需要标题（微头条 / 百家动态都无标题）。"""
    return bool(form_meta(platform, form).get("need_title", True))


def can_publish(platform: str, form: str = "") -> bool:
    """这个「平台 · 形态」有没有网页端自动发布入口（百家动态只能人工发）。"""
    return bool(form_meta(platform, form).get("publish", True))


def form_hint(platform: str, form: str = "") -> str:
    """形态说明：追加到正文提示词末尾，避免短文被写成大文章（或反之）。"""
    if form_key(platform, form) != "micro":
        return ""
    return _FORM_HINT.get((str(platform or ""), "micro"), _FORM_HINT_DEFAULT)


def prompt_stages(platform: str, form: str = "") -> tuple:
    return PROMPT_STAGES if need_title(platform, form) else ("body", "polish", "fix")


def prompt_key(platform: str, form: str, stage: str) -> str:
    """分块提示词的键名：prompt.<平台>.<形态>.<阶段>。"""
    plat = str(meta(platform).get("key") or platform or "")
    return f"prompt.{plat}.{form_key(platform, form)}.{stage}"


def style_brief(platform: str, form: str = "") -> str:
    """给改写提示词用的风格说明（注入 {{platform_style}}）。"""
    if form_key(platform, form) == "micro":
        return _MICRO_BRIEF.get(str(platform or ""), _MICRO_BRIEF_DEFAULT)
    return _STYLE_BRIEF.get(str(platform or ""), "")


def meta(platform: str) -> Dict[str, str]:
    return dict(PLATFORMS.get(str(platform or ""), {}))


def title_of(platform: str) -> str:
    return meta(platform).get("title") or str(platform or "")


def form_title(platform: str, form: str = "") -> str:
    return str(form_meta(platform, form).get("title") or "")


def label_of(platform: str, form: str = "") -> str:
    """给页面/日志看的一行名字：今日头条·微头条 / 百家号·图文。"""
    name = title_of(platform)
    sub = form_title(platform, form)
    return f"{name}·{sub}" if sub else name


def dir_of(platform: str, form: str = "") -> str:
    """交付目录短名：多形态平台按形态分目录（tt / tt-wtt），单形态保持老目录。"""
    forms = forms_of(platform)
    if len(forms) > 1:
        return str(form_meta(platform, form).get("dir")
                   or meta(platform).get("dir") or str(platform or ""))
    return meta(platform).get("dir") or str(platform or "")


def key_of_dir(name: str) -> str:
    """目录短名反查平台 key（tt → toutiao）。"""
    for key, item in PLATFORMS.items():
        if item["dir"] == name:
            return key
    return ""


def explain(source: Any) -> Dict[str, Any]:
    """给前端展示用：这条来源会被判成什么、要出几版。"""
    norm = normalize_source(source)
    return {
        "source": str(source or ""),
        "norm": norm,
        "targets": targets_for(source),
        "reason": "改写后头条版与百家版各出一份（不受原文来源限制）",
    }
