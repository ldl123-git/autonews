"""关键词提取：不依赖第三方分词库，基于 n-gram 词频 + 位置/标题权重。"""
import re
from typing import Dict, Iterable, List, Tuple

from ..config import MEDIA_KEYWORD_COUNT, MEDIA_TITLE_QUERY_MAX

# 常见虚词 / 无信息量单字（作为 n-gram 的首尾或整体会被降权/丢弃）
STOP_CHARS = set(
    "的了是在和与及也就都而或把你我他她它们这那个们到说都为会能给等对很还吧呢啊吗嘛哦"
    "上下中前后里外时被将把从向以之其所又再最更已未不无有做个种条只第年月日时分秒"
)
STOP_WORDS = {
    "可以", "这个", "那个", "什么", "怎么", "为什么", "如何", "一个", "一种", "一些",
    "目前", "现在", "已经", "表示", "认为", "进行", "出现", "相关", "有关", "方面",
    "记者", "编辑", "报道", "来源", "声明", "未经", "授权", "转载", "原文", "阅读",
    "以下", "上述", "其中", "因此", "但是", "不过", "而且", "并且", "同时", "此外",
    "搜狐", "查看更多", "澎湃新闻", "客户端", "关注我们", "推荐阅读", "精彩推荐",
}
# 单字不成词
MIN_LEN = 2
MAX_LEN = 4

# 以量词 / 单位 / 时间结尾的多半不是有效关键词（万元、三年、10日…）
DROP_SUFFIX = set("元万亿年月日号点次件起名位岁件条块角分")
_DIGIT = re.compile(r"[0-9０-９]")

_CJK = re.compile(r"[\u4e00-\u9fa5]+")
_LATIN = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")


def _segments(text: str) -> List[str]:
    return _CJK.findall(text or "")


def _score_tokens(text: str, weight: float, counter: Dict[str, float]) -> None:
    for seg in _segments(text):
        n = len(seg)
        if n < MIN_LEN:
            continue
        for size in range(MIN_LEN, min(MAX_LEN, n) + 1):
            for i in range(n - size + 1):
                gram = seg[i:i + size]
                if gram[0] in STOP_CHARS or gram[-1] in STOP_CHARS:
                    continue
                if gram in STOP_WORDS or len(set(gram)) == 1:
                    continue
                if gram[-1] in DROP_SUFFIX or _DIGIT.search(gram):
                    continue
                # 位置权重：越靠前越重要
                counter[gram] = counter.get(gram, 0.0) + weight * (1.0 + 0.6 / (1 + i / 40))
    for w in _LATIN.findall(text or ""):
        counter[w.lower()] = counter.get(w.lower(), 0.0) + weight


def _demote_fragments(counter: Dict[str, float]) -> Dict[str, float]:
    """降权"碎片词"：如「害人」只是「受害人 / 加害人 / 嫌疑人」的切片。

    判据：该词的词频基本被更长的包含词解释掉（coverage ≥ 60%），说明它本身不是独立词。
    """
    words = list(counter.keys())
    for w in words:
        if len(w) >= MAX_LEN:
            continue
        coverage = sum(v for h, v in counter.items() if len(h) > len(w) and w in h)
        if coverage >= 0.6 * counter[w]:
            counter[w] *= 0.35
    return counter


def _overlap(a: str, b: str) -> int:
    """最长公共子串长度（词长 ≤4，暴力即可）。"""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best


def _drop_shifted(cands: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """去掉「移位碎片」：同长度且重叠 len-1 的候选其实是同一个词的切片，只留高分者。

    n-gram 会把「女子酒馆」同时切成「女子酒馆 / 子酒馆砸 / 酒馆砸毁 / 馆砸毁多」，
    全塞进检索词只会污染搜索结果（「万余」「万转到银」这类碎片同理）。
    """
    kept: List[Tuple[str, float]] = []
    for word, score in cands:
        if any(len(k) == len(word) and _overlap(k, word) >= len(word) - 1 for k, _ in kept):
            continue
        kept.append((word, score))
    return kept


def _filter_overlap(cands: List[Tuple[str, float]]) -> List[str]:
    """去掉互相包含的低分词（"成都" 与 "成都市" 只留高分者）。"""
    picked: List[str] = []
    for word, _ in cands:
        if any(word in p or p in word for p in picked):
            continue
        picked.append(word)
    return picked


def extract(title: str, text: str, category: str = "", limit: int = 0) -> List[str]:
    """从标题 + 正文抽取关键词，标题权重更高。"""
    limit = limit or MEDIA_KEYWORD_COUNT
    counter: Dict[str, float] = {}
    _score_tokens(title or "", 3.0, counter)          # 标题权重最高
    _score_tokens(category or "", 2.0, counter)       # 分类作为补充信号
    _score_tokens((text or "")[:3000], 1.0, counter)  # 正文只取前 3000 字

    counter = _demote_fragments(counter)
    cands = sorted(counter.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    words = _filter_overlap(cands)
    # 长词优先展示，但若不足则补 2 字词
    if len(words) < limit:
        extra = [w for w, _ in cands if w not in words]
        words += extra
    out: List[str] = []
    seen = set()
    for w in words:
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def title_terms(title: str, limit: int = 4) -> List[str]:
    """标题核心实词：从标题里挑信息量最高的词，并保持其在标题中的先后顺序。

    用于两件事：①拼成「核心短语」当搜索词（比整句标题更容易命中同类素材）；
    ②作为相关度打分的命中项（见 search.match_score）。
    """
    base = title_head(title) or title or ""
    counter: Dict[str, float] = {}
    _score_tokens(base, 3.0, counter)
    if not counter:
        return []
    counter = _demote_fragments(counter)
    cands = sorted(counter.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    words = _filter_overlap(cands)[: max(limit * 2, 6)]
    # 标题里越靠前的词越可能是主语/主体，检索时优先
    words.sort(key=lambda w: base.find(w) if base.find(w) >= 0 else 999)
    return words[:limit]


def title_entities(title: str, limit: int = 4) -> List[str]:
    """标题里的「事件实体」：稳定、完整、能代表这件事的词（蔷琵 / 浙江 / 台风）。

    与 title_terms 的区别：title_terms 取的是 n-gram 高分碎片（一次只偷 / 次只偷一），
    适合拼检索词，但拿去做「相关性判据」会把视频标题里零散的命中算成相关。
    这里取的是**互相不重叠、彼此竞争后胜出的实词**，用于判断「这个视频是不是在讲
    同一件事」—— 视频 UP 主的措辞与新闻标题差异很大，只有实体层面对得上才算相关。
    """
    base = full_title(title) or title or ""
    counter: Dict[str, float] = {}
    _score_tokens(base, 1.0, counter)
    if not counter:
        return []
    counter = _demote_fragments(counter)
    # 同分时位置靠前的优先（标题开头往往是主语 / 事件主体），再用 _drop_shifted
    # 去掉滑动窗口切出来的移位碎片（来势 / 势汹 / 汹汹 只留一个）
    cands = sorted(counter.items(),
                   key=lambda kv: (-kv[1], base.find(kv[0]) if base.find(kv[0]) >= 0 else 999))
    words = _filter_overlap(_drop_shifted(cands))[:limit]
    words.sort(key=lambda w: base.find(w) if base.find(w) >= 0 else 999)
    return words


# 标题里被引号 / 书名号包裹的内容：新闻的事件专名几乎都在里面
_QUOTED = re.compile(
    r"[“”\"'‘’《》〈〉「」『』【]"
    r"([^“”\"'‘’《》〈〉「」『』】]{2,14})"
    r"[”\"'‘’《》〈〉「」『』】]")


def strong_entities(title: str, limit: int = 3) -> List[str]:
    """标题里引号括起来的事件专名：「蔷琶」「务工敲诈」「湖北积雪」……

    这类词是判定「同一个事件」的最硬凭据 —— 视频标题只要提到它，
    基本就是在讲这件事（新闻标题的引号内容通常是台风名、案件名、专项名）。
    """
    out: List[str] = []
    for s in _QUOTED.findall(title or ""):
        s = s.strip()
        if len(s) < 2 or s in out:
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def build_queries(title: str, text: str, category: str = "",
                  limit: int = 3, custom: Iterable[str] = ()) -> List[str]:
    """生成用于素材搜索的查询词：完整标题 → 标题主体 → 正文关键词兜底。"""
    title_qs, keyword_qs = build_query_tiers(title, text, category, limit, custom)
    return title_qs + keyword_qs


def build_query_tiers(title: str, text: str, category: str = "",
                      limit: int = 3, custom: Iterable[str] = ()
                      ) -> Tuple[List[str], List[str]]:
    """把查询词分成两层：`title_qs`（标题层）与 `keyword_qs`（关键词层）。

    标题层 —— **不加分词**：直接用清洗后的完整标题去检索，命中同源报道 /
    同事件素材的概率最高；只有在完整标题过长时才会退化到「标题主体」。
    关键词层 —— 正文 / 分类提取的词，泛化严重，**只用于图片兜底**，
    绝不当作视频检索词（关键词搜出来的视频基本跑题）。
    """
    title_qs: List[str] = []
    full = full_title(title)
    if full:
        title_qs.append(full)
    head = title_head(title, limit=40)
    if head and head not in title_qs:
        title_qs.append(head)

    words = [w for w in custom if w] or extract(title, text, category, limit=limit)
    keyword_qs: List[str] = [w for w in words if w and w not in title_qs]
    # 关键词层内部去重，并剔除与标题层重复的词（自定义词优先于正文提取词）
    seen = set(title_qs)
    dedup: List[str] = []
    for w in keyword_qs:
        if w in seen:
            continue
        seen.add(w)
        dedup.append(w)
    return title_qs, dedup


def full_title(title: str, limit: int = 0) -> str:
    """完整标题检索词：只做标点清洗与站点尾巴剥离，**不分词、不截断主体**。

    搜索引擎对长标题的召回确实更弱，但一旦用分词后的短词去搜，
    图片、视频就会变成「概念图 / 同名八竿子打不着」的内容 —— 与标题的匹配
    度反而更低。所以默认按完整标题检索，宁可结果少也要保证「图不对文」不发生。
    """
    limit = limit or MEDIA_TITLE_QUERY_MAX
    s = re.sub(r"[“”\"'‘’《》〈〉「」『』【】\[\]()（）|｜]", " ", title or "")
    s = re.sub(r"[·、，。！？；;：:!?]", " ", s)
    parts = [p.strip() for p in s.split() if p.strip()]
    out = " ".join(parts)
    out = re.sub(r"\s+", " ", out).strip(" -—_,，。、")
    return out[:limit] if limit else out


def title_head(title: str, limit: int = 40) -> str:
    """标题主体：去掉括注、引号与站点尾巴，保留能代表事件的最短完整片段。"""
    s = re.sub(r"[“”\"'‘’《》〈〉「」『』【】\[\]()（）|｜]", " ", title or "")
    parts = [p.strip(" -—·、,，。") for p in re.split(r"[:：,，。！？；;]", s)]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    head = parts[0]
    # 「官方通报」「突发」这类前置引导语信息量不足，补上后续片段再检索
    if len(head) < 8 and len(parts) > 1:
        head = f"{head} {parts[1]}"
    head = re.sub(r"\s+", " ", head).strip(" -—·、,，。")
    return head[:limit]
