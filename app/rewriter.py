"""文章改写引擎：标题改写 → 正文深度改写 → 自然化润色。

三轮提示词都在「系统设置 → 内容改写」里维护，这里只负责编排：
- 取原文（raw_cache 正文优先，退回文章标题/摘要）
- 每轮调用 llm.achat()，失败按 rewrite.max_retries 重试（带退避）
- 中间稿与终稿都写进 article_rewrites，articles.rewrite_status 只存进度
队列范式与 fetcher / media 一致：asyncio.Queue + N 个常驻 worker。
"""
import asyncio
import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from . import llm, sensitive, settings, similarity, style
from .config import CACHE_DIR
from .db import get_conn, write_conn

TITLE_STYLES = ["头条风格", "百家号风格", "知乎风格"]
STAGE_LABEL = {"title": "标题改写", "body": "正文改写", "polish": "自然化润色"}


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 输出清洗 / 解析
def clean_output(text: str) -> str:
    """去掉模型常见的包裹物：代码围栏、前后说明、多余空行。"""
    out = str(text or "").strip()
    out = re.sub(r"^```[a-zA-Z]*\s*", "", out)
    out = re.sub(r"\s*```$", "", out)
    out = re.sub(r"^(好的|以下是|以下是改写后的稿件|改写如下)[:：]?\s*", "", out)
    return out.strip()


def parse_titles(text: str) -> List[Dict[str, str]]:
    """把「一行一条」的标题输出解析成 [{style, title}]。"""
    out: List[Dict[str, str]] = []
    for i, raw in enumerate((text or "").splitlines()):
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^#+\s*", "", line)                       # Markdown 标题号
        line = re.sub(r"^[-*]\s*", "", line)                     # 列表符号
        line = re.sub(r"^\d+[\.、\)）]\s*", "", line)             # 纯序号
        line = re.sub(r"^标题\s*\d+\s*[（(][^）)]*[）)]\s*[:：]?\s*", "", line)  # 标题1（头条风格）：
        line = re.sub(r"^标题\s*\d+\s*[:：]?\s*", "", line)
        line = re.sub(r"^标题[^：:]{0,12}[:：]\s*", "", line)                 # 标题甲：
        line = line.strip().strip('"“”「」').strip('*# ')
        if not line:
            continue
        style = TITLE_STYLES[i] if i < len(TITLE_STYLES) else f"候选 {i + 1}"
        out.append({"style": style, "title": line})
    return out[:6]


# ---------------------------------------------------------------- 原文读取
def load_source(article_id: int) -> Dict[str, Any]:
    """取改写的原始素材：优先 raw_cache 正文，其次是文章自带字段。"""
    with get_conn() as conn:
        art = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        if not art:
            raise LookupError("文章不存在")
        cache = conn.execute(
            "SELECT * FROM raw_cache WHERE article_id=? ORDER BY id DESC LIMIT 1", (article_id,)
        ).fetchone()
    text = ""
    if cache:
        text = _cache_text(cache)
    if not text:
        text = str(art["title"] or "")
    return {"article": dict(art), "cache_id": cache["id"] if cache else None, "text": text}


def _cache_text(cache: Any) -> str:
    """raw_cache.path 存的是完整文件路径，正文在 JSON 的 text 字段。"""
    raw = cache["path"] if "path" in cache.keys() else ""
    if not raw:
        return ""
    p = Path(raw)
    if not p.is_absolute():
        p = CACHE_DIR / p
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("text") or "")


# ---------------------------------------------------------------- 落库
def _mark(article_id: int, status: str, error: str = "", rewrite_id: Optional[int] = None) -> None:
    with write_conn() as conn:
        cols = "rewrite_status=?, rewrite_error=?, rewrite_at=?"
        args: List[Any] = [status, (error or "")[:300], _now()]
        if rewrite_id:
            cols += ", rewrite_id=?"
            args.append(rewrite_id)
        args.append(article_id)
        conn.execute(f"UPDATE articles SET {cols} WHERE id=?", args)


def _save_result(payload: Dict[str, Any]) -> int:
    now = _now()
    with write_conn() as conn:
        cur = conn.execute(
            "INSERT INTO article_rewrites (article_id, cache_id, target_platform, content_form,"
            " variant_group, origin_title, origin_text, titles,"
            " used_title, draft_text, final_text, words, stage, status, attempts, error, provider,"
            " model, elapsed, dup_ratio, dup_detail, dup_rounds, dup_checked, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [payload["article_id"], payload.get("cache_id"),
             payload.get("target_platform", ""), payload.get("content_form", "article"),
             payload.get("variant_group", ""),
             payload.get("origin_title", ""),
             payload.get("origin_text", ""), payload.get("titles", "[]"), payload.get("used_title", ""),
             payload.get("draft_text", ""), payload.get("final_text", ""), payload.get("words", 0),
             payload.get("stage", ""), payload.get("status", "success"), payload.get("attempts", 0),
             (payload.get("error") or "")[:400], payload.get("provider", ""), payload.get("model", ""),
             payload.get("elapsed", 0),
             float(payload.get("dup_ratio") or 0), payload.get("dup_detail") or "[]",
             int(payload.get("dup_rounds") or 0), int(payload.get("dup_checked") or 0),
             now, now],
        )
        return int(cur.lastrowid or 0)


def _update_result(rewrite_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with write_conn() as conn:
        conn.execute(f"UPDATE article_rewrites SET {sets}, updated_at=? WHERE id=?",
                     list(fields.values()) + [_now(), rewrite_id])


def _hit_signature(report: Dict[str, Any]) -> Tuple[Any, ...]:
    """命中指纹（词 + 次数）：同一批词原样留着，说明模型绕不开，再修也没用。"""
    return tuple(sorted((str(h.get("word") or ""), int(h.get("count") or 0))
                        for h in (report.get("blocked") or [])))


def _merge_hits(first: List[Dict[str, Any]],
                live: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """首检命中 + 最终命中：已清掉的标 resolved=True，仍命中的标 resolved=False。

    修复成功时最终命中通常为空，页面仍要能展示「当初命中了什么、哪些已经改掉」。
    """
    live_keys = {(str(h.get("word")), str(h.get("field"))) for h in live}
    merged: List[Dict[str, Any]] = []
    seen = set()
    for h in first:
        key = (str(h.get("word")), str(h.get("field")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(h, resolved=key not in live_keys))
    for h in live:
        key = (str(h.get("word")), str(h.get("field")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(h, resolved=False))
    return merged


def _too_similar(a: Dict[str, Any], b: Dict[str, Any], threshold: float = 0.86) -> bool:
    """两个平台版本是否像同一篇稿子：标题相同，或开头 900 字高度重合。"""
    ta, tb = str(a.get("text") or ""), str(b.get("text") or "")
    if not ta or not tb:
        return False
    if a.get("title") and str(a.get("title")) == str(b.get("title")):
        return True
    import difflib
    return difflib.SequenceMatcher(None, ta[:900], tb[:900]).ratio() >= threshold


# ---------------------------------------------------------------- 引擎
class RewriteEngine:
    def __init__(self) -> None:
        self.queue: "asyncio.Queue[int]" = asyncio.Queue()
        self.concurrency = 1
        self.running = 0
        self._workers: List["asyncio.Task[None]"] = []

    async def start(self) -> None:
        if self._workers:
            return
        self.concurrency = max(1, settings.get_int("rewrite.article_concurrency", 2))
        for _ in range(self.concurrency):
            self._workers.append(asyncio.create_task(self._worker()))

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        self._workers.clear()

    def enqueue(self, article_ids: List[int]) -> int:
        ids = [int(i) for i in article_ids if i]
        if not ids:
            return 0
        for aid in ids:
            self.queue.put_nowait(aid)
        with write_conn() as conn:
            conn.executemany(
                "UPDATE articles SET rewrite_status='queued', rewrite_error='' WHERE id=?",
                [(aid,) for aid in ids],
            )
        return len(ids)

    def stats(self) -> Dict[str, int]:
        return {"queued": self.queue.qsize(), "running": self.running}

    async def _worker(self) -> None:
        while True:
            article_id = await self.queue.get()
            self.running += 1
            try:
                await self._rewrite_one(article_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _mark(article_id, "failed", f"改写异常：{exc}")
            finally:
                self.running -= 1
                self.queue.task_done()

    # ------------------------------------------------------------ 单篇
    async def _rewrite_one(self, article_id: int) -> None:
        """调度：两个平台都产出，且只产出一组形态。

        平台：头条版 + 百家版各一份，不看原文来源（文本是改写过的，两边都能发，
        见 app/style.py）。形态：原文 <400 字只改写短稿（头条微头条 + 百家动态），
        ≥400 字只改写图文（头条图文 + 百家图文），另一组形态直接不产出 ——
        少一半改写量，排版里那半组自然是空的。
        任一版本成功就算这篇成功，失败的版本单独记录原因。
        """
        started = time.time()
        cfg = llm.resolve("article")
        if not cfg:
            _mark(article_id, "failed", "没有可用的文章模型，请先在「系统设置 → 大模型配置」里接入一个")
            return
        try:
            src = await asyncio.to_thread(load_source, article_id)
        except LookupError as exc:
            _mark(article_id, "failed", str(exc))
            return
        if not src["text"].strip():
            _mark(article_id, "failed", "没有可改写的正文，请先抓取原文")
            return

        targets = style.targets_for(src["article"].get("source"))
        # 形态由原文正文字数决定：头条 <300 字走微头条（无标题、只存草稿）
        chars = style.count_chars(src["text"])
        plans = [(p, style.form_for(p, chars)) for p in targets]
        labels = {f"{p}:{f}": style.label_of(p, f) for p, f in plans}
        group = f"{article_id}:{int(started)}"      # 同一批次的多个平台版本归一组
        _mark(article_id, "running", "")
        results: Dict[str, Dict[str, Any]] = {}     # key = platform:form
        errs: List[str] = []
        for platform, form in plans:
            key = f"{platform}:{form}"
            try:
                res = await self._rewrite_variant(
                    article_id, src, cfg, platform, form, group, started)
                if res.get("ok"):
                    results[key] = res
                elif res.get("error"):
                    errs.append(f"{labels[key]}：{res['error']}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单个平台失败不影响另一个
                errs.append(f"{labels[key]}：{exc}")
        # 多个版本如果标题相同或正文高度重合，说明模型偷懒，针对性重跑一次
        if len(results) > 1:
            diff_min = settings.get_float("rewrite.diff_min", 0.86)
            for key in list(results):
                others = {k: v for k, v in results.items() if k != key}
                if not any(_too_similar(results[key], o, diff_min) for o in others.values()):
                    continue
                hit = results[key]
                try:
                    retry = await self._rewrite_variant(
                        article_id, src, cfg, hit["platform"], hit["form"], group, started,
                        avoid=others)
                    if retry.get("ok"):
                        results[key] = retry
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    errs.append(f"{labels[key]}（差异化重写）：{exc}")
        # 文章级敏感词结论：只要有一个版本合格就算通过；全都因敏感词失败才标 blocked，
        # 其它原因（模型不可用 / 抓取失败）保持为空，别让看板误标成敏感词问题
        sens = [str((v or {}).get("sensitive_status") or "") for v in results.values()]
        if sens:
            status = "fixed" if "fixed" in sens else "pass"
        elif "敏感词" in "；".join(errs):
            status = "blocked"
        else:
            status = ""
        with write_conn() as conn:
            conn.execute("UPDATE articles SET sensitive_status=? WHERE id=?", (status, article_id))

        if results:
            _mark(article_id, "success", "；".join(errs)[:300])
            await self._autofinish(article_id)
        else:
            _mark(article_id, "failed", ("；".join(errs) or "没有产出任何版本")[:300])

    @staticmethod
    async def _autofinish(article_id: int) -> None:
        """改写完成即交付；素材还没到就先出文字版，素材到了会覆盖重写。"""
        try:
            from . import finish
            await asyncio.to_thread(finish.export_article, article_id)
        except Exception:  # noqa: BLE001 - 交付失败不影响改写结果
            pass

    async def _rewrite_variant(self, article_id: int, src: Dict[str, Any], cfg: Dict[str, Any],
                               platform: str, form: str, group: str, started: float,
                               avoid: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        """跑一个「平台 · 形态」版本；成功返回 {ok, title, text, rewrite_id}。

        短稿（微头条 / 百家动态）没有标题：跳过第 1 轮，used_title 留空。
        avoid = 另一版本已产出的内容（标题 + 正文），用于「两版太像」时的差异化重写。
        """
        brief = style.style_brief(platform, form)
        pname = style.label_of(platform, form)
        need_title = style.need_title(platform, form)
        retries = max(0, settings.get_int("rewrite.max_retries", 2))
        keep_title = settings.get_bool("rewrite.keep_title", False) and need_title
        do_polish = settings.get_bool("rewrite.enable_polish", True)
        avoid_list = [v for v in (avoid or {}).values() if v]
        avoid_titles = "、".join(str(v.get("title") or "") for v in avoid_list
                                if v.get("title"))
        avoid_text = next((str(v.get("text") or "") for v in avoid_list if v.get("text")), "")

        payload: Dict[str, Any] = {
            "article_id": article_id,
            "cache_id": src["cache_id"],
            "target_platform": platform,
            "content_form": form,
            "variant_group": group,
            "origin_title": src["article"].get("title") or "",
            "origin_text": src["text"],
            "provider": cfg.get("provider") or "",
            "model": cfg.get("model") or "",
            "status": "running",
            "attempts": 0,
            "titles": "[]",
            "used_title": "",
            "draft_text": "",
            "final_text": "",
            "sensitive_status": "",
            "sensitive_hits": "[]",
            "sensitive_rounds": 0,
            "sensitive_checked_at": "",
            "dup_ratio": 0.0,
            "dup_detail": "[]",
            "dup_rounds": 0,
            "dup_checked": 0,
        }
        rewrite_id = _save_result(payload)
        _mark(article_id, "running", "", rewrite_id)

        # ---- 第 1 轮：标题改写（微头条跳过）。只出目标平台这一条 ----
        titles: List[Dict[str, str]] = []
        if need_title and not keep_title:
            title_hint = (f"\n\n【目标平台】{pname}\n【平台风格】{brief}\n"
                          "只输出 1 行标题，不要输出其它平台风格的标题，不要任何解释。")
            if avoid_titles:
                title_hint += (f"\n【必须避开】另一版本已使用的标题：{avoid_titles}。"
                               "本次标题在句式、关键词和切入点上都要与它不同。")
            raw, err, tries = await self._step(
                cfg, settings.render_prompt_for(platform, form, "title", src["text"]), retries,
                system=str(cfg.get("system_prompt") or ""), suffix=title_hint)
            payload["attempts"] += tries
            if err:
                return self._fail_variant(rewrite_id, "title", err, payload, started)
            titles = self._pick_titles(parse_titles(raw), platform)
            payload["titles"] = json.dumps(titles, ensure_ascii=False)
            payload["used_title"] = titles[0]["title"] if titles else ""
            payload["stage"] = "body"

        # ---- 第 2 轮：正文深度改写。输入同样是原文 ----
        # 标题已单独生成，这里只作为追加约束挂在提示词末尾，
        # 不塞进 {{article_text}}，避免污染模板里「原文：」的语义。
        suffix = ""
        if titles:
            suffix = (f"\n\n【已确定的标题】{titles[0]['title']}\n"
                      "（标题已在第 1 轮单独生成，正文需与标题描述一致，且不要再输出任何标题）")
        # 平台风格一定要追加：老库里用户保存过的提示词模板里没有 {{platform_style}}，
        # 只有追加这段，分平台风格与两版差异化要求才真正生效。
        # 降重规则直接追加在提示词末尾：老库里用户保存的模板没有这条，
        # 而平台查重（百家号「高度相似」限流）恰恰按连续相同片段判定。
        suffix += f"\n【目标平台】{pname}\n【平台风格】{brief}\n{style.diff_requirement()}"
        suffix += ("\n【降重硬规则】不得与原文出现连续 10 个字以上相同的表述；"
                   "判决书、通报、当事人原话必须转述（换主语、换句式、合并或拆分），禁止整段引用；"
                   "段落顺序可按叙事逻辑重排，别保留原文的段落骨架。")
        if not need_title:
            suffix += "\n【形态】" + style.form_hint(platform, form)
        if avoid_text:
            # 只给开头一段做「避让参照」，给全文会让模型倾向抄结构
            suffix += ("\n【另一版本的开头（仅供避让，禁止照抄其句式与段落顺序）】\n"
                       + avoid_text[:600])
        draft, err, tries = await self._step(
            cfg, settings.render_prompt_for(platform, form, "body", src["text"]), retries,
            system=str(cfg.get("system_prompt") or ""), suffix=suffix)
        payload["attempts"] += tries
        if err:
            return self._fail_variant(rewrite_id, "body", err, payload, started)
        payload["draft_text"] = clean_output(draft)
        payload["stage"] = "polish"

        # ---- 第 3 轮：自然化润色。输入是第 2 轮的初稿，不是原文 ----
        final_text = payload["draft_text"]
        if do_polish:
            polished, err, tries = await self._step(
                cfg, settings.render_prompt_for(platform, form, "polish", payload["draft_text"]),
                retries, system=str(cfg.get("system_prompt") or ""))
            payload["attempts"] += tries
            if err:
                # 润色失败不整篇判死：退回初稿，仍算成功
                payload["error"] = f"润色失败，已用深度改写稿：{err}"
            else:
                final_text = clean_output(polished)
        payload["final_text"] = final_text
        if need_title:
            if not payload.get("used_title"):
                payload["used_title"] = payload["origin_title"]
        else:
            payload["used_title"] = ""          # 微头条无标题

        # ---- 降重：与原文连续相同的片段过多，平台会判「高度相似」限流 ----
        final_text = await self._dedup_pass(cfg, src["text"], final_text, retries, payload)
        payload["final_text"] = final_text

        # ---- 敏感词校验：命中就带着清单回炉，通过才允许进入排版/交付 ----
        title = str(payload.get("used_title") or "")
        ok, final_text, title, report = await self._sensitive_rounds(
            cfg, platform, form, pname, article_id, rewrite_id, final_text, title, retries, payload)
        payload["used_title"] = title
        payload["final_text"] = final_text
        payload["words"] = len(re.sub(r"\s", "", final_text))
        payload["elapsed"] = round(time.time() - started, 2)
        if not ok and settings.get_bool("sensitive.block_export", True):
            return self._fail_variant(
                rewrite_id, "sensitive",
                str(payload.get("error") or
                    f"敏感词未通过（修复 {payload.get('sensitive_rounds', 0)} 轮）："
                    f"{sensitive.brief(report)}"),
                payload, started)
        payload["status"] = "success"
        payload["stage"] = ""
        _update_result(rewrite_id, **{k: v for k, v in payload.items() if k != "article_id"})
        return {"ok": True, "rewrite_id": rewrite_id, "platform": platform, "form": form,
                "title": title, "text": final_text,
                "dup_ratio": payload.get("dup_ratio") or 0,
                "sensitive_status": payload.get("sensitive_status") or ""}

    async def _dedup_pass(self, cfg: Dict[str, Any], origin: str, text: str,
                          retries: int, payload: Dict[str, Any],
                          rounds: Optional[int] = None) -> str:
        """与原文比字面重合率，超阈值就带着重复句清单回炉，直到降下来或轮数用尽。

        平台（百家号 / 头条）判「与站内现有内容高度相似」看的是连续相同片段，
        所以统计的也是连续重合字数占比，而不是语义相似度；不通过不阻断出稿，
        只在记录里留痕，前端会显示「重合 X%，可能被判相似」。
        """
        if not settings.get_bool("rewrite.dedup_enabled", True):
            payload.update({"dup_ratio": 0.0, "dup_detail": "[]", "dup_rounds": 0, "dup_checked": 0})
            return text
        limit = settings.get_float("rewrite.dedup_ratio", 0.20)
        if rounds is None:
            rounds = settings.get_int("rewrite.dedup_rounds", 2)
        rounds = max(0, int(rounds))
        min_run = settings.get_int("rewrite.dedup_minrun", similarity.DEFAULT_MIN_RUN)
        base = settings.PROMPT_DEDUP_BASE
        extra = (settings.get_setting("rewrite.prompt_dedup") or "").strip()
        if extra == settings.PROMPT_DEDUP_V1:      # 老库遗留的初版模板，不算用户自定义
            extra = ""

        def _note(msg: str) -> None:
            prev = str(payload.get("error") or "")
            payload["error"] = (prev + "；" if prev else "") + msg

        report = similarity.compare(origin, text, min_run)
        best_text, best_report = text, report      # 模型偶尔越改越像，保留重合最低的那一版
        used = 0
        while report["ratio"] > limit and used < rounds:
            frags = "\n".join(f"- {s}" for s in (report.get("fragments") or [])) \
                or "-（正文中与原文重合的段落，逐段换一种说法）"
            prompt = settings.render_template(base, text, fragments=frags, origin_text=origin)
            if extra:
                prompt += "\n\n【补充要求】\n" + extra
            out, err, tries = await self._step(
                cfg, prompt, retries, system=str(cfg.get("system_prompt") or ""))
            payload["attempts"] += tries
            used += 1
            payload["dup_rounds"] = used
            if err:
                _note(f"降重第 {used} 轮失败：{err}")
                break
            cleaned = clean_output(out)
            if not cleaned:
                break
            text = cleaned
            report = similarity.compare(origin, text, min_run)
            if report["ratio"] < best_report["ratio"]:
                best_text, best_report = text, report

        payload["dup_ratio"] = float(best_report.get("ratio") or 0)
        payload["dup_detail"] = json.dumps(best_report.get("details") or [], ensure_ascii=False)
        payload["dup_checked"] = 1
        if best_report["ratio"] > limit and used >= rounds:
            _note(f"降重 {used} 轮后与原文仍重合 {best_report['ratio'] * 100:.0f}%"
                  f"（阈值 {limit * 100:.0f}%）")
        return best_text

    async def dedup_only(self, rewrite_id: int, rounds: Optional[int] = None) -> Dict[str, Any]:
        """对已产出的稿件单独再跑降重，不重新改写正文。

        模型改写波动大，一次不一定压到阈值以下；留一个可重复触发的手动入口，
        每跑一次都与原文比一遍，只保留重合更低的那一版，并重跑交付。
        """
        d = detail(rewrite_id)
        if not d:
            return {"ok": False, "error": "改写记录不存在"}
        origin = str(d.get("origin_text") or "")
        text = str(d.get("final_text") or d.get("draft_text") or "")
        if not origin or not text:
            return {"ok": False, "error": "没有可比对的原文或成稿"}
        cfg = llm.resolve("article")
        if not cfg:
            return {"ok": False, "error": "没有可用的文章模型，请先在「系统设置 → 大模型配置」里接入"}
        started = time.time()
        retries = max(0, settings.get_int("rewrite.max_retries", 2))
        payload: Dict[str, Any] = {"attempts": 0, "error": "", "dup_ratio": 0.0,
                                   "dup_detail": "[]", "dup_rounds": 0, "dup_checked": 0}
        new_text = await self._dedup_pass(cfg, origin, text, retries, payload, rounds=rounds)
        _update_result(
            rewrite_id, final_text=new_text, words=len(re.sub(r"\s", "", new_text)),
            dup_ratio=payload["dup_ratio"], dup_detail=payload["dup_detail"],
            dup_rounds=payload["dup_rounds"], dup_checked=1,
            error=(payload.get("error") or "")[:400],
            elapsed=round(time.time() - started, 2),
        )
        if d.get("status") == "success" and d.get("article_id"):
            await self._autofinish(int(d["article_id"]))
        return {"ok": True, **(detail(rewrite_id) or {})}

    async def _sensitive_rounds(self, cfg: Dict[str, Any], platform: str, form: str,
                                pname: str, article_id: int, rewrite_id: int, text: str,
                                title: str, retries: int,
                                payload: Dict[str, Any]) -> Tuple[bool, str, str, Dict[str, Any]]:
        """润色后校验 → 命中就带清单回炉重写 → 直到通过或轮数用尽。

        返回 (是否通过, 正文, 标题, 最终报告)。每轮都写 sensitive_checks 留痕：
        round=0 是润色后首检，round=N 是第 N 轮修复后的复查，页面据此展示修复过程。
        标题命中先尝试重新生成，仍命中再把标题本身改掉。
        """
        if not settings.get_bool("sensitive.enabled", True):
            payload["sensitive_status"] = "pass"
            payload["sensitive_checked_at"] = _now()
            return True, text, title, {"ok": True, "hits": [], "blocked": [], "warn": []}

        report = sensitive.check(text, title)
        self._log_check(article_id, rewrite_id, 0, platform, form, report, text, "")
        if not report["ok"] and any(h.get("field") == "标题" for h in report["blocked"]):
            title = await self._fix_title(cfg, platform, form, pname, rewrite_id,
                                          title, report, retries, payload)
            report = sensitive.check(text, title)
            self._log_check(article_id, rewrite_id, 0, platform, form, report, text, "")
        # 首检命中记下来：修成功后页面上仍能看到「当初命中了什么、哪些已经清掉」
        first_hits = [dict(h) for h in report.get("hits") or []]

        auto_fix = settings.get_bool("sensitive.auto_fix", True)
        max_rounds = max(0, settings.get_int("sensitive.max_rounds", 3))
        # 涉政命中（机构名、党派、人名）改不动，多轮只会烧钱：单独限轮
        political = any(str(h.get("category") or "") == "政治" for h in report["blocked"])
        if political:
            max_rounds = min(max_rounds, max(0, settings.get_int("sensitive.political_rounds", 1)))
        rounds = 0
        prev_sig = _hit_signature(report)
        while not report["ok"] and auto_fix and rounds < max_rounds:
            before = text
            body_hits = [h for h in report["blocked"] if h.get("field") != "标题"]
            prompt = settings.render_prompt_for(
                platform, form, "fix", text, hits=sensitive.prompt_list(body_hits or report["blocked"]))
            fixed, err, tries = await self._step(
                cfg, prompt, retries, system=str(cfg.get("system_prompt") or ""))
            payload["attempts"] = int(payload.get("attempts") or 0) + tries
            if err:
                payload["error"] = f"敏感词修复失败：{err}"
                break
            candidate = clean_output(fixed)
            if not candidate or candidate == before:
                break
            rounds += 1
            text = candidate
            report = sensitive.check(text, title)
            self._log_check(article_id, rewrite_id, rounds, platform, form, report, before, text)
            payload["sensitive_rounds"] = rounds
            sig = _hit_signature(report)
            if sig == prev_sig:          # 还是同一批词：模型绕不开，继续烧钱没意义
                break
            prev_sig = sig

        ok = bool(report["ok"])
        payload["sensitive_status"] = ("pass" if ok and rounds == 0
                                       else ("fixed" if ok else "blocked"))
        payload["sensitive_hits"] = json.dumps(
            _merge_hits(first_hits, report.get("hits") or []), ensure_ascii=False)
        payload["sensitive_rounds"] = rounds
        payload["sensitive_checked_at"] = _now()
        if not ok:
            msg = f"敏感词未通过（修复 {rounds} 轮）：{sensitive.brief(report)}"
            old = str(payload.get("error") or "")
            if old and "敏感词修复失败" in old:
                msg += "；" + old
            if any(str(h.get("tier")) == "case" for h in report["blocked"]):
                msg += "；含案件必要表述，建议人工确认后发布"
            elif political:
                msg += "；涉政内容，建议人工处理或不发布"
            payload["error"] = msg
        return ok, text, title, report

    async def _fix_title(self, cfg: Dict[str, Any], platform: str, form: str, pname: str,
                         rewrite_id: int, title: str, report: Dict[str, Any],
                         retries: int, payload: Dict[str, Any]) -> str:
        """标题命中：先直接改标题（跑偏风险最低），改不动再带禁止词重新生成。

        重新生成必须过「不跑题」校验：实测模型会凭空编出另一个案件的标题，
        而新标题恰好不含敏感词，光靠敏感词校验根本拦不住。
        """
        title_hits = [h for h in report["blocked"] if h.get("field") == "标题"]
        banned = "、".join(h["word"] for h in title_hits)
        if not banned:
            return title

        # 1) 直接改标题：只清命中词，句式与事实保持原样
        prompt = settings.render_prompt_for(
            platform, form, "fix", title, hits=sensitive.prompt_list(title_hits))
        fixed, err, tries = await self._step(
            cfg, prompt, retries, system=str(cfg.get("system_prompt") or ""),
            suffix="\n\n【本次只改标题】上面这段是新闻标题，只输出改写后的标题一行，不要写正文、不要解释。")
        payload["attempts"] = int(payload.get("attempts") or 0) + tries
        if not err:
            cleaned = clean_output(fixed)
            cand = cleaned.splitlines()[0].strip() if cleaned else ""
            if cand and self._title_ok(cand, title) and sensitive.check("", cand)["ok"]:
                self._accept_title(payload, platform, cand)
                return cand

        # 2) 直改不行，再按标题提示词重新生成（明确禁止命中词）
        hint = (f"\n\n【目标平台】{pname}\n【必须避开的词】{banned}"
                "（标题里不得以任何形式出现这些词，包括拼音、谐音与拆字）\n"
                "只输出 1 行标题，不要编号、不要解释。")
        raw, err, tries = await self._step(
            cfg, settings.render_prompt_for(platform, form, "title",
                                            str(payload.get("origin_text") or "")[:1500]),
            retries, system=str(cfg.get("system_prompt") or ""), suffix=hint)
        payload["attempts"] = int(payload.get("attempts") or 0) + tries
        if not err:
            cand = next((t["title"] for t in parse_titles(raw) if t.get("title")), "")
            if cand and self._title_ok(cand, title) and sensitive.check("", cand)["ok"]:
                self._accept_title(payload, platform, cand)
                return cand
        return title

    @staticmethod
    def _title_ok(cand: str, old: str) -> bool:
        """改写/重生成的标题不能跑题：长度合理 + 与原标题字符重合度达标。"""
        import difflib
        cand = str(cand or "").strip()
        if not (8 <= len(cand) <= 60):
            return False
        return difflib.SequenceMatcher(None, str(old or ""), cand).ratio() >= 0.25

    @staticmethod
    def _accept_title(payload: Dict[str, Any], platform: str, cand: str) -> None:
        payload["used_title"] = cand
        payload["titles"] = json.dumps(
            [{"style": style.meta(platform).get("style") or "标题", "title": cand}],
            ensure_ascii=False)

    @staticmethod
    def _log_check(article_id: int, rewrite_id: int, round_no: int, platform: str,
                   form: str, report: Dict[str, Any], before: str, after: str) -> None:
        """留痕：每轮的命中词 + 待修稿 + 修后稿（截断 5000 字）。"""
        try:
            with write_conn() as conn:
                conn.execute(
                    "INSERT INTO sensitive_checks (article_id, rewrite_id, round, platform,"
                    " content_form, status, hits, before_text, after_text, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [article_id, rewrite_id, round_no, platform, form,
                     "pass" if report.get("ok") else "fail",
                     json.dumps(report.get("hits") or [], ensure_ascii=False),
                     str(before or "")[:5000], str(after or "")[:5000], _now()])
        except Exception:      # noqa: BLE001 - 留痕失败不影响改写结果
            pass

    @staticmethod
    def _pick_titles(items: List[Dict[str, str]], platform: str) -> List[Dict[str, str]]:
        """只保留目标平台那一条标题。

        老提示词会让模型一次给头条/百家/知乎三种风格；按 style 标签挑出目标平台的，
        挑不到就退回第一条 —— 绝不把「百家号风格」的标题用到头条版上。
        """
        want = str(style.meta(platform).get("style") or "")
        for t in items:
            if want and want in str(t.get("style") or ""):
                return [t]
        return items[:1]

    def _fail_variant(self, rewrite_id: int, stage: str, error: str,
                      payload: Dict[str, Any], started: float) -> Dict[str, Any]:
        """单个平台版本失败：只把这条记录标失败，不影响另一个平台。"""
        payload.update({"status": "failed", "stage": stage, "error": error,
                        "elapsed": round(time.time() - started, 2)})
        _update_result(rewrite_id, **{k: v for k, v in payload.items() if k != "article_id"})
        return {"ok": False, "rewrite_id": rewrite_id, "error": error}

    async def _step(self, cfg: Dict[str, Any], prompt: str,
                    retries: int, system: str = "", suffix: str = "") -> Tuple[str, str, int]:
        """跑一轮模型调用，带退避重试。返回 (输出, 错误, 尝试次数)。

        prompt 由调用方按「平台 · 形态 · 阶段」渲染好传入（见 settings.render_prompt_for）。
        """
        prompt = str(prompt or "")
        if suffix:
            prompt += suffix
        last = ""
        for attempt in range(retries + 1):
            try:
                out = await llm.achat(cfg, prompt, system=system)
                if out and out.strip():
                    return out.strip(), "", attempt + 1
                last = "模型返回为空"
            except httpx.HTTPStatusError as exc:
                last = f"HTTP {exc.response.status_code}：{(exc.response.text or '')[:200]}"
            except httpx.HTTPError as exc:
                last = f"网络错误：{exc.__class__.__name__}"
            except ValueError as exc:
                return "", str(exc), attempt + 1  # 提示词/配置类错误，重试没意义
            except Exception as exc:  # noqa: BLE001
                last = f"{exc.__class__.__name__}：{exc}"
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
        return "", last or "调用失败", retries + 1


engine = RewriteEngine()


# ---------------------------------------------------------------- 查询
def latest(article_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM article_rewrites WHERE article_id=? ORDER BY id DESC LIMIT 1",
            (article_id,)).fetchone()
    return _present(row) if row else None


def detail(rewrite_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM article_rewrites WHERE id=?", (rewrite_id,)).fetchone()
    return _present(row) if row else None


def delete(rewrite_id: int) -> bool:
    with write_conn() as conn:
        row = conn.execute("SELECT article_id FROM article_rewrites WHERE id=?",
                           (rewrite_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM article_rewrites WHERE id=?", (rewrite_id,))
        conn.execute(
            "UPDATE articles SET rewrite_status='', rewrite_error='', rewrite_id=NULL WHERE id=?",
            (row["article_id"],))
    return True


def variants(article_id: int) -> List[Dict[str, Any]]:
    """这篇原文产出的各版本，每个「平台 · 形态」取最新一条。

    原文 <400 字只有短稿（头条微头条 + 百家动态），≥400 字只有图文
    （头条图文 + 百家图文）—— 只产出一组，另一组保持为空。
    没有 target_platform 的历史记录（改造前的单版本）按头条图文处理，
    这样老数据在页面上仍能看到。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM article_rewrites WHERE article_id=? ORDER BY id DESC", (article_id,)
        ).fetchall()
    latest: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        d = _present(r)
        plat = str(d.get("target_platform") or "")
        if not plat:
            continue
        key = f"{plat}:{d['content_form']}"
        if key not in latest:
            latest[key] = d
    order = ["toutiao:article", "toutiao:micro", "baijia:article", "baijia:micro"]
    out = [latest[k] for k in order if k in latest]
    for k, v in latest.items():              # 以后接的新平台按出现顺序补在后面
        if k not in order:
            out.append(v)
    if out:
        return out
    return [_present(rows[0])] if rows else []


def _present(row: Any) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["titles"] = json.loads(d.get("titles") or "[]")
    except (TypeError, ValueError):
        d["titles"] = []
    d["titles"] = d["titles"] if isinstance(d["titles"], list) else []
    try:
        d["sensitive_hits"] = json.loads(d.get("sensitive_hits") or "[]")
    except (TypeError, ValueError):
        d["sensitive_hits"] = []
    d["sensitive_hits"] = d["sensitive_hits"] if isinstance(d["sensitive_hits"], list) else []
    d["sensitive_status"] = str(d.get("sensitive_status") or "")
    d["sensitive_label"] = sensitive.status_label(d["sensitive_status"])
    d["sensitive_rounds"] = int(d.get("sensitive_rounds") or 0)
    # 与原文的字面重合度：平台查重（百家号「高度相似」限流）按连续相同片段判定
    d["dup_ratio"] = round(float(d.get("dup_ratio") or 0), 4)
    d["dup_rounds"] = int(d.get("dup_rounds") or 0)
    d["dup_checked"] = bool(int(d.get("dup_checked") or 0))
    try:
        d["dup_detail"] = json.loads(d.get("dup_detail") or "[]")
    except (TypeError, ValueError):
        d["dup_detail"] = []
    d["dup_detail"] = d["dup_detail"] if isinstance(d["dup_detail"], list) else []
    threshold = settings.get_float("rewrite.dedup_ratio", 0.20)
    d["dup_threshold"] = round(threshold, 4)
    if not d["dup_checked"]:
        d["dup_level"] = ""                      # 老数据没测过
    elif d["dup_ratio"] > threshold * 1.5:
        d["dup_level"] = "bad"
    elif d["dup_ratio"] > threshold:
        d["dup_level"] = "warn"
    else:
        d["dup_level"] = "ok"
    # 老数据没有 content_form：按图文处理，前端不用再判空
    d["content_form"] = str(d.get("content_form") or "article")
    d["need_title"] = style.need_title(str(d.get("target_platform") or ""), d["content_form"])
    return d