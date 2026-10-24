"""敏感词校验：加载 Sensitive-lexicon 词库 → 归一化 → AC 自动机匹配 → 分级判定。

设计取舍（与 8.7 万条词库的实际情况对应）：

* **按文件分级**。词库是宽口径过滤库，`广告类型.txt` 里就有「网络 / 客服 / 招聘」，
  `涉枪涉爆` 里含「解放军」，全量启用会把正常民生稿全判成不合格。因此按文件名
  分四档：block（拦截）/ warn（只提示）/ url（只扫链接）/ off（默认关闭的大库）。
* **归一化只处理插入型干扰**：空白、零宽字符、`·*-_.` 与引号。保留中文句读，
  避免跨句拼接造成误报；拼音与谐音变体词库本身已经收录，程序不再造词。
* **纯 Python Aho-Corasick**：只加载启用档位（默认约 2 万条），构建 <1s，
  单篇 2000 字扫描 <5ms；进程内缓存，词库 mtime / 设置变化时自动重建。
* **英文词要求词边界**：词库里有 `sm` / `bt` / `js` / `gv` 这类两字母词，
  不判边界会把 `smash`、`switch` 全命中。
* **白名单双用途**：词条级（完全等于白名单则整条忽略）与片段级
  （命中前后 30 字内出现白名单词 → 降级为提示，不参与拦截）。

对外只需要 `check(text, title)`，其余是配置与自检工具。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from . import config, settings

# ---------------------------------------------------------------- 分级
#: block = 硬敏感（色情/暴恐/违禁/政治）；case = 案件必要表述（仍拦，但单独标记便于人工判断）
TIER_RANK = {"block": 0, "case": 0, "warn": 1, "url": 2, "off": 3}
TIER_LABEL = {"block": "拦截", "case": "案件", "warn": "提示", "url": "网址", "off": "备用"}
DEFAULT_TIERS = "block,warn,url"

#: 文件名关键词 → 分级。顺序敏感：先把 off / url 的特例挑出来，再判 block / warn。
_FILE_TIERS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("off", ("广告", "gfw", "网易", "零时", "tencent", "新思想")),
    ("url", ("网址", "url", "domain", "域名")),
    ("block", ("色情", "暴恐", "涉枪", "涉爆", "反动", "政治")),
    ("warn", ("民生", "贪腐", "补充", "其他", "covid", "疫情")),
)

#: 内置白名单：词库里混进来的正常政务 / 司法 / 地名 / 中性词，词条级直接不加载。
#: 实测 36 条成稿里，「政府」3 次、「北京」8 次、「法院」7 次、「中央」7 次全是正常表述，
#: 不剔除会让每篇文章都要多跑一轮修复（而模型也无法改动这些事实专名）。
DEFAULT_WHITELIST = frozenset({
    # 政务 / 司法机构（社会新闻必现，不构成平台风险）
    "政府", "市政府", "法院", "检察院", "公安", "警方", "中央", "国务院",
    "人大", "政协", "市委", "纪委", "监委", "信访办", "街道办",
    # 地名（词库里的疫情 / 事件词条）
    "北京", "上海", "广州", "深圳", "武汉", "西安", "杭州", "南京", "成都",
    "乌鲁木齐", "拉萨", "新疆", "西藏",
    # 单独出现不足以判低俗的日常词（色情词库收录过宽）
    "自拍", "按摩",
    # 实测 warn 噪声词：一篇稿子里出现 7 次「法院」「中央」「北京」只是干扰，
    # 这几个人名 / 法律 / 行业词同理，只提示不给任何决策价值
    "起诉", "纠纷", "判决", "通报", "白皮书", "大师", "洪志", "法正",
    "车库", "疫苗", "核酸", "隔离", "封控",
})

#: 案件必要表述：新闻报道里描述案情必须出现，与「色情渲染」不同。
#: 仍然拦截（平台会限流），但单独归类，修不动时提示「转人工」而不是当普通违规。
CASE_WORDS = frozenset({
    "发生关系", "性关系", "偷拍", "偷录", "裸照", "不雅视频", "嫖资",
    "卖淫", "嫖娼", "奸情", "赤身", "下身", "尸体", "血迹", "尸检",
    "性侵", "猥亵", "强奸", "轮奸",
})

#: 词库文件 → 分类，用于页面展示「命中的是哪一类风险」
_CATEGORIES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("政治", ("政治", "反动", "gfw", "新思想")),
    ("色情", ("色情",)),
    ("暴恐", ("暴恐",)),
    ("违禁品", ("涉枪", "涉爆")),
    ("垃圾导流", ("广告", "网址", "url", "域名")),
    ("社会", ("民生", "贪腐", "covid", "疫情")),
)

#: 上下文干扰：命中词前后紧邻这些字时，说明它只是更大词语的一部分。
#: 「both」= 前缀与后缀要同时命中才算误报（如 阴部 + 前「区/地」+ 后「分」= 其它地区阴部分有阵雨），
#: 避免把「阴部分泌物」这种真实敏感表述也放过去。
_DISTRACT: Dict[str, Dict[str, Any]] = {
    "中共": {"prefix": ("其", "集", "手", "心", "空", "同")},      # 其中共济 / 集中共建
    "阴部": {"prefix": ("区", "地", "到", "于"), "suffix": ("分",), "both": True},
    "公开信": {"suffix": ("息",)},                                  # 公开信息
    "入联": {"suffix": ("合", "席", "络", "盟", "系", "调", "通", "接")},  # 入驻联合 / 进入联调
    "群交": {"suffix": ("流", "易", "往", "谈", "换", "相", "接", "互")},  # 人群交流 / 集群交相
    "法正": {"prefix": ("方", "办", "做", "说", "想", "合", "章")},  # 方法正确
    "自治": {"suffix": ("区", "州", "县", "市")},
}

#: 归一化时丢弃的「插入型」字符（不含句读，避免跨句拼接误报）
_DROP = set(
    " \t\r\n\u3000\u00a0\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"
    "·•＊*-_—–~`^|／/\\\"'“”‘’「」『』"
)
_SENT_END = "。！？!?\n"
_MAX_TEXT = 30000          # 单次扫描上限，超长正文截断（正常稿件远小于此）

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s，。；、）)\]】\"']+", re.I)
_DOMAIN_RE = re.compile(
    r"\b[a-z0-9][a-z0-9\-]{1,62}\.(?:com|net|org|cn|cc|info|top|xyz|vip|club|site|online|biz|me|tv|co)\b",
    re.I,
)

_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {}
_PRELOAD_STARTED = False

# 配置与词库都带 2 秒缓存：check() 会被每篇文章、每一轮调用，
# 每次都开 SQLite 连接 + 遍历词库目录（WSL 挂载盘 stat 很慢）会白花上百毫秒。
_CONF_TTL = 2.0
_KEY_TTL = 2.0
_CONF: Dict[str, Any] = {"at": 0.0, "data": {}}


def _conf() -> Dict[str, str]:
    now = time.time()
    if _CONF["data"] and now - float(_CONF["at"] or 0) < _CONF_TTL:
        return _CONF["data"]
    data = settings.all_settings()
    _CONF.update(at=now, data=data)
    return data


def _cfg(key: str, default: str = "") -> str:
    return str(_conf().get(key) or default)


def _cfg_int(key: str, default: int = 0) -> int:
    try:
        return int(float(_cfg(key).strip() or default))
    except (TypeError, ValueError):
        return default


def _cfg_bool(key: str, default: bool = False) -> bool:
    raw = _cfg(key).strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 归一化 / 工具
def normalize(text: str) -> Tuple[str, List[int]]:
    """全角→半角、去插入型干扰、小写，并返回「归一化位置 → 原串位置」映射。"""
    chars: List[str] = []
    idx: List[int] = []
    for i, ch in enumerate(str(text or "")):
        if ch in _DROP:
            continue
        folded = unicodedata.normalize("NFKC", ch)
        if not folded:
            continue
        for c in folded:
            if c in _DROP:
                continue
            chars.append(c.lower())
            idx.append(i)
    return "".join(chars), idx


def _is_ascii_word(word: str) -> bool:
    return bool(word) and all(ord(c) < 128 for c in word)


def _boundary(norm: str, start: int, end: int) -> bool:
    """英文词两侧不能是字母数字（避免 sm → smash、js → json）。"""
    left = norm[start - 1] if start > 0 else ""
    right = norm[end] if end < len(norm) else ""
    return not (left.isascii() and left.isalnum()) and not (right.isascii() and right.isalnum())


def _snippet(text: str, start: int, end: int, width: int = 30) -> str:
    lo = max(0, start - width)
    hi = min(len(text), end + width)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()


# ---------------------------------------------------------------- 词库加载
def _lexicon_dir() -> Path:
    raw = _cfg("sensitive.lexicon_dir").strip()
    return Path(raw).expanduser() if raw else Path(config.SENSITIVE_DIR)


def _iter_files(root: Path) -> List[Path]:
    """词库目录 → txt 列表：优先 <root>/Vocabulary，没有再递归找。"""
    vocab = root / "Vocabulary"
    base = vocab if vocab.is_dir() else root
    if not base.is_dir():
        return []
    try:
        return sorted(p for p in base.rglob("*.txt") if p.is_file())
    except OSError:
        return []


def _tier_of(path: Path) -> str:
    name = path.stem.lower()
    for tier, keys in _FILE_TIERS:
        if any(k.lower() in name for k in keys):
            return tier
    return "off"            # 没归类的文件一律当备用库，默认不启用


def _category_of(source: str) -> str:
    """词条来源（文件名）→ 风险分类，页面按这个分组展示。"""
    name = str(source or "").lower()
    for category, keys in _CATEGORIES:
        if any(k.lower() in name for k in keys):
            return category
    return "其他"


def _enabled_tiers() -> set:
    raw = (_cfg("sensitive.tiers") or DEFAULT_TIERS).lower()
    picked = {x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()}
    return picked & set(TIER_RANK) or {"block", "warn", "url"}


def _whitelist_raw() -> str:
    return _cfg("sensitive.whitelist")


def _user_whitelist() -> set:
    """设置页维护的白名单（片段级豁免只认这一份）。"""
    out = set()
    for line in _whitelist_raw().splitlines():
        w = line.strip().lstrip("-*• ").strip()
        if w and not w.startswith("#"):
            out.add(w.lower())
    return out


def _whitelist_set() -> set:
    """词条级白名单 = 内置正常词 + 用户白名单（整条不加载，既不拦也不提示）。"""
    out = set(_user_whitelist())
    if _cfg_bool("sensitive.default_whitelist", True):
        out |= set(DEFAULT_WHITELIST)
    return out


def _distracted(word: str, norm: str, start: int, end: int) -> bool:
    """命中词是不是更大词语的一部分（子串误报）。"""
    rule = _DISTRACT.get(word)
    if not rule:
        return False
    pre = norm[start - 1] if start > 0 else ""
    suf = norm[end] if end < len(norm) else ""
    hit_pre = bool(pre) and pre in rule.get("prefix", ())
    hit_suf = bool(suf) and suf in rule.get("suffix", ())
    if rule.get("both"):
        return hit_pre and hit_suf
    return hit_pre or hit_suf


def _clean_word(raw: str) -> str:
    w = str(raw or "").strip().strip("\ufeff").strip()
    if not w or w.startswith("#") or w.startswith("//"):
        return ""
    return w.lower()


def _noise(word: str) -> bool:
    """词库里的说明性长句 / 描述行（如「安眠酮 代药物毒品类：血浆」）。"""
    if len(word) > 24:
        return True
    if "：" in word or "=" in word:
        return True
    if " " in word and len(word) > 10:
        return True
    return False


def _load_words(root: Path) -> Tuple[Dict[str, Tuple[str, str]], List[str], List[Dict[str, Any]]]:
    tiers = _enabled_tiers()
    min_len = max(1, _cfg_int("sensitive.min_len", 2))
    whitelist = _whitelist_set()
    words: Dict[str, Tuple[str, str]] = {}
    urls: List[str] = []
    files: List[Dict[str, Any]] = []

    for path in _iter_files(root):
        tier = _tier_of(path)
        if tier not in tiers:
            continue
        file_tier = tier                 # 单文件统计用原档位，词条可能被改成 case
        try:
            lines = path.read_text("utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        kept = 0
        for raw in lines:
            w = _clean_word(raw)
            if not w or w in whitelist or _noise(w):
                continue
            if tier == "url":
                urls.append(w)
                kept += 1
                continue
            floor = 1 if tier == "block" else min_len   # 拦截档保留单字脏字，避免漏放
            if len(w) < floor:
                continue
            if tier == "block" and w in CASE_WORDS:
                tier = "case"                        # 案件必要表述：仍拦，单独归类
            old = words.get(w)
            if old and TIER_RANK[old[0]] <= TIER_RANK[tier]:
                continue
            words[w] = (tier, path.stem)
            kept += 1
        files.append({"file": path.name, "tier": file_tier, "count": kept,
                      "label": TIER_LABEL.get(file_tier, file_tier)})

    for raw in _cfg("sensitive.extra_words").splitlines():
        w = _clean_word(raw)
        if w and w not in whitelist and not _noise(w):
            words[w] = ("block", "自定义追加")
    if any(w for w in words.values()):
        files.append({"file": "自定义追加", "tier": "block",
                      "count": sum(1 for v in words.values() if v[1] == "自定义追加"),
                      "label": "拦截"})
    return words, urls, files


# ---------------------------------------------------------------- AC 自动机
class _Automaton:
    __slots__ = ("goto", "fail", "out", "size")

    def __init__(self, words: Iterable[str]) -> None:
        goto: List[Dict[str, int]] = [{}]
        fail: List[int] = [0]
        out: List[List[str]] = [[]]
        for w in words:
            node = 0
            for ch in w:
                nxt = goto[node].get(ch)
                if nxt is None:
                    goto.append({})
                    fail.append(0)
                    out.append([])
                    nxt = len(goto) - 1
                    goto[node][ch] = nxt
                node = nxt
            if w not in out[node]:
                out[node].append(w)
        queue: deque = deque()
        for u in goto[0].values():
            fail[u] = 0
            queue.append(u)
        while queue:
            r = queue.popleft()
            for ch, u in list(goto[r].items()):
                queue.append(u)
                f = fail[r]
                while f and ch not in goto[f]:
                    f = fail[f]
                v = goto[f].get(ch, 0)
                fail[u] = 0 if v == u else v
                if out[fail[u]]:
                    out[u] = out[u] + out[fail[u]]
        self.goto, self.fail, self.out = goto, fail, out
        self.size = len(goto)

    def find(self, text: str) -> Iterator[Tuple[str, int, int]]:
        node = 0
        goto, fail, out = self.goto, self.fail, self.out
        for i, ch in enumerate(text):
            while node and ch not in goto[node]:
                node = fail[node]
            node = goto[node].get(ch, 0)
            outs = out[node]
            if outs:
                end = i + 1
                for w in outs:
                    yield w, end - len(w), end


def _cache_key() -> Tuple[Any, ...]:
    root = _lexicon_dir()
    try:
        stamps = tuple(sorted((p.name, int(p.stat().st_mtime)) for p in _iter_files(root)))
    except OSError:
        stamps = ()
    return (str(root), stamps, tuple(sorted(_enabled_tiers())),
            _cfg_int("sensitive.min_len", 2), _whitelist_raw(),
            _cfg("sensitive.extra_words"), _cfg_bool("sensitive.default_whitelist", True))


def _machine() -> Dict[str, Any]:
    now = time.time()
    with _LOCK:
        if _CACHE.get("auto") and now - float(_CACHE.get("key_at") or 0) < _KEY_TTL:
            return _CACHE
    key = _cache_key()
    with _LOCK:
        if _CACHE.get("key") == key:
            _CACHE["key_at"] = now
            return _CACHE
        started = time.time()
        root = _lexicon_dir()
        words, urls, files = _load_words(root)
        auto = _Automaton(words.keys())
        _CACHE.clear()
        _CACHE.update({
            "key": key, "key_at": now, "auto": auto, "words": words, "urls": urls,
            "files": files,             "built_at": started,
            "build_ms": round((time.time() - started) * 1000, 1),
            "whitelist": _user_whitelist(),        # 片段级豁免只认用户白名单
            "whitelist_total": len(_whitelist_set()),
        })
        return _CACHE


def invalidate() -> None:
    """丢弃配置与词库缓存，下次校验时按新设置重建（不阻塞当前请求）。"""
    with _LOCK:
        _CACHE.clear()
        _CONF.update(at=0.0, data={})


def reload() -> Dict[str, Any]:
    """立即重建并返回统计（设置页「重新加载词库」/ 词库更新后调用）。"""
    invalidate()
    return stats()


def warmup() -> None:
    """后台预热：进程启动后先建好自动机，避免第一篇文章等构建。"""
    global _PRELOAD_STARTED
    if _PRELOAD_STARTED:
        return
    _PRELOAD_STARTED = True
    threading.Thread(target=stats, name="sensitive-warmup", daemon=True).start()


def stats() -> Dict[str, Any]:
    m = _machine()
    return {
        "dir": str(_lexicon_dir()),
        "words": len(m["words"]),
        "urls": len(m["urls"]),
        "nodes": m["auto"].size,
        "build_ms": m["build_ms"],
        "tiers": sorted(_enabled_tiers()),
        "enabled": _cfg_bool("sensitive.enabled", True),
        "whitelist": int(m.get("whitelist_total") or 0),
        "distractors": len(_DISTRACT),
        "files": m["files"],
    }


# ---------------------------------------------------------------- 校验
def _empty(reason: str) -> Dict[str, Any]:
    return {"ok": True, "status": "pass", "hits": [], "blocked": [], "case": [], "warn": [],
            "words": [], "categories": [], "reason": reason, "elapsed_ms": 0, "tiers": []}


def _exempt(snippet: str, whitelist: set) -> bool:
    low = snippet.lower()
    return any(w in low for w in whitelist) if whitelist else False


def _drop_nested(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """丢掉「被更长命中完整包含」的短词：法轮 / 轮功 都包含于 法轮功，展示时只留最长。"""
    kept: List[Dict[str, Any]] = []
    for hit in items:
        word = hit["word"]
        nested = any(
            other["word"] != word and word in other["word"]
            and other["field"] == hit["field"]
            for other in items
        )
        if not nested:
            kept.append(hit)
    return kept


def _scan(text: str, field: str, machine: Dict[str, Any],
          hits: Dict[Tuple[str, str], Dict[str, Any]]) -> None:
    if not text:
        return
    body = text[:_MAX_TEXT]
    norm, idx = normalize(body)
    if not norm:
        return
    whitelist = machine.get("whitelist") or set()
    for word, start, end in machine["auto"].find(norm):
        if not word:
            continue
        if _is_ascii_word(word) and not _boundary(norm, start, end):
            continue
        if _distracted(word, norm, start, end):
            continue                      # 子串误报：其中共济 / 阴部分有阵雨 / 公开信息
        tier, source = machine["words"].get(word, ("block", ""))
        o_start = idx[start] if start < len(idx) else 0
        o_end = (idx[end - 1] + 1) if 0 <= end - 1 < len(idx) else len(body)
        snippet = _snippet(body, o_start, o_end)
        if tier in ("block", "case") and _exempt(snippet, whitelist):
            tier = "warn"                     # 白名单词出现在同句 → 只提示，不拦
        hit = hits.get((word, field))
        if hit:
            hit["count"] += 1
            if len(hit["snippets"]) < 3 and snippet not in hit["snippets"]:
                hit["snippets"].append(snippet)
        else:
            hits[(word, field)] = {"word": word, "tier": tier, "file": source,
                                   "category": _category_of(source),
                                   "field": field, "count": 1, "snippets": [snippet]}


def _scan_urls(text: str, field: str, urls: List[str],
               hits: Dict[Tuple[str, str], Dict[str, Any]]) -> None:
    if not text or not urls:
        return
    targets = [u.lower() for u in _URL_RE.findall(text[: _MAX_TEXT])]
    targets += [d.lower() for d in _DOMAIN_RE.findall(text[: _MAX_TEXT])]
    if not targets:
        return
    seen = set()
    for target in targets:
        for word in urls:
            if word in seen or word not in target:
                continue
            seen.add(word)
            hit = hits.get((word, field))
            if hit:
                hit["count"] += 1
            else:
                hits[(word, field)] = {"word": word, "tier": "block", "file": "非法网址",
                                       "category": "垃圾导流", "field": field,
                                       "count": 1, "snippets": [target[:60]]}


def check(text: str, title: str = "") -> Dict[str, Any]:
    """校验一段正文（可带标题）。

    返回 ``ok`` 只表示「没有拦截级命中」；``warn`` 里的词只做展示。
    """
    if not _cfg_bool("sensitive.enabled", True):
        return _empty("敏感词校验已关闭")
    started = time.time()
    machine = _machine()
    hits: Dict[Tuple[str, str], Dict[str, Any]] = {}
    _scan(str(text or ""), "正文", machine, hits)
    if title and _cfg_bool("sensitive.check_title", True):
        _scan(str(title), "标题", machine, hits)
    if _cfg_bool("sensitive.check_url", True):
        _scan_urls(str(text or ""), "正文", machine.get("urls") or [], hits)

    items = _drop_nested(sorted(
        hits.values(),
        key=lambda h: (TIER_RANK.get(h["tier"], 9), h["field"] != "正文",
                       -h["count"], h["word"])))
    blocked = [h for h in items if h["tier"] in ("block", "case")]
    case_hits = [h for h in items if h["tier"] == "case"]
    warn = [h for h in items if h["tier"] == "warn"]
    strict = _cfg_bool("sensitive.strict", False)
    ok = not blocked and not (strict and warn)
    words: List[str] = []
    for h in (blocked if not strict else blocked + warn):
        if h["word"] not in words:
            words.append(h["word"])
    return {
        "ok": ok,
        "status": "pass" if ok else "fail",
        "hits": items,
        "blocked": blocked,
        "case": case_hits,          # 案件必要表述（已含在 blocked 里，单独标出来给人工看）
        "warn": warn,
        "words": words,
        "categories": sorted({str(h.get("category")) for h in items if h.get("category")}),
        "elapsed_ms": round((time.time() - started) * 1000, 1),
        "build_ms": machine.get("build_ms"),
        "tiers": sorted(_enabled_tiers()),
        "text_len": len(str(text or "")),
    }


# ---------------------------------------------------------------- 展示 / 提示词
def prompt_list(hits: List[Dict[str, Any]], limit: int = 30) -> str:
    """把命中词整理成给模型的清单：词 + 次数 + 上下文片段。"""
    lines: List[str] = []
    for i, h in enumerate(hits[:limit], 1):
        snip = (h.get("snippets") or [""])[0]
        lines.append(f"{i}. 「{h['word']}」（出现 {h.get('count', 1)} 次）上下文：…{snip}…")
    if len(hits) > limit:
        lines.append(f"（另有 {len(hits) - limit} 个词未列出，同样必须清除）")
    return "\n".join(lines)


def brief(report: Dict[str, Any], limit: int = 8) -> str:
    """一句话概括命中词，用于错误信息与页面标签。"""
    words: List[str] = []
    for h in report.get("blocked") or []:
        words.append(f"{h['word']}×{h['count']}" if int(h.get("count") or 1) > 1 else h["word"])
    text = "、".join(words[:limit])
    return text + ("…" if len(words) > limit else "")


def status_label(status: str,) -> str:
    return {"pass": "通过", "fixed": "已修复", "blocked": "未通过",
            "waived": "人工放行"}.get(str(status or ""), "未校验")


# ---------------------------------------------------------------- 自检 CLI
def _cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.sensitive", description="敏感词校验自检")
    parser.add_argument("--text", default="", help="待校验文本")
    parser.add_argument("--file", default="", help="从文件读取待校验文本")
    parser.add_argument("--title", default="", help="同时校验标题")
    parser.add_argument("--stats", action="store_true", help="查看词库统计")
    parser.add_argument("--reload", action="store_true", help="重建词库缓存")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)

    from .db import init_db
    init_db()                                   # 自检时也保证 settings 表存在

    if args.reload:
        reload()
    if args.stats:
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
        return 0
    text = args.text
    if args.file:
        text = Path(args.file).read_text("utf-8", errors="ignore")
    if not text:
        parser.print_help()
        return 2
    report = check(text, args.title)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"结论：{status_label(report['status'])} · 拦截 {len(report['blocked'])} 个 "
              f"· 提示 {len(report['warn'])} 个 · {report['elapsed_ms']}ms")
        for h in report["hits"]:
            print(f"[{TIER_LABEL.get(h['tier'], h['tier'])}] {h['word']} "
                  f"← {h['file']} · {h['field']} ×{h['count']} · {(h['snippets'] or [''])[0]}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(_cli())
