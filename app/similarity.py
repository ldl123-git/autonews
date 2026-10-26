"""改写稿与原文的字面重合度检测（平台查重口径）。

百家号 / 头条判「与站内现有内容高度相似」看的是**连续相同片段**，不是语义相似度：
连续 8~15 个字和人已发布内容一致，就可能被判重复、限制推荐。所以这里不用
difflib 的整体相似度，而是滑动 n-gram 标出改写稿里与原文逐字重合的位置，给出：

* ratio      —— 重合字数 / 改写稿字数（降重是否达标的判断依据）
* max_run    —— 最长连续重合长度（平台最容易抓的特征）
* fragments  —— 重合比例高的句子（交给模型点名重写，比让它自己找有效得多）
"""
import re
from typing import Any, Dict, List

# 连续 N 个字完全相同才算重合：太小会把「警方通报」「法院认为」这类常用说法、
# 还有机构全名算进去，太大又抓不到整句照抄。13 大致是「整句照搬」的量级。
DEFAULT_MIN_RUN = 13

# 去掉空白与标点后再比对：平台查重同样忽略标点差异
_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)
_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;\n])")


def normalize(text: str) -> str:
    return _STRIP.sub("", str(text or ""))


def compare(origin: str, final: str, min_run: int = DEFAULT_MIN_RUN) -> Dict[str, Any]:
    """对比改写稿与原文，返回重合率、最长连续重合与需要重写的句子。"""
    o = normalize(origin)
    f = normalize(final)
    total = len(f)
    result: Dict[str, Any] = {"ratio": 0.0, "chars": 0, "total": total,
                              "max_run": 0, "fragments": [], "details": []}
    if not o or not f:
        return result
    n = max(6, int(min_run or DEFAULT_MIN_RUN))

    grams = {o[i:i + n] for i in range(max(0, len(o) - n + 1))}
    hit = bytearray(total)
    for i in range(max(0, total - n + 1)):
        if f[i:i + n] in grams:
            for j in range(i, i + n):
                hit[j] = 1

    chars = int(sum(hit))
    run = best = 0
    for v in hit:
        run = run + 1 if v else 0
        if run > best:
            best = run

    # 逐句统计：一句话里超过 60% 的字与原文重合，基本就是照抄，点名让模型重写
    details: List[Dict[str, Any]] = []
    pos = 0
    for raw in _SENT_SPLIT.split(str(final or "")):
        seg = normalize(raw)
        if not seg:
            continue
        start, end = pos, pos + len(seg)
        pos = end
        covered = int(sum(hit[start:min(end, total)]))
        if len(seg) >= n and covered >= max(n, len(seg) * 0.5):
            details.append({"text": raw.strip(), "share": round(covered / len(seg), 3)})
    details.sort(key=lambda x: (-x["share"], -len(x["text"])))

    result.update({
        "ratio": round(chars / total, 4) if total else 0.0,
        "chars": chars,
        "max_run": best,
        "fragments": [d["text"] for d in details[:12]],
        "details": details[:12],
    })
    return result


def brief(report: Dict[str, Any]) -> str:
    """一行摘要，写进改写记录的错误/提示位与日志。"""
    ratio = float(report.get("ratio") or 0)
    return (f"与原文重合 {ratio * 100:.0f}%（{report.get('chars', 0)}/{report.get('total', 0)} 字，"
            f"最长连续 {report.get('max_run', 0)} 字）")
