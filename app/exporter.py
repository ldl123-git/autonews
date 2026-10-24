"""采集结果导出：output/<标题>/ 目录 + 原文 txt + meta.json。"""
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import OUTPUT_DIR
from .db import get_conn, write_conn

# 目录名非法字符（Windows / Linux 通用）
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')
_WS = re.compile(r"\s+")
DIR_NAME_LIMIT = 40          # 目录名最长字符数（超了自动截取）

TXT_HEADER = (
    "标题：{title}\n"
    "来源：{source}\n"
    "作者：{author}\n"
    "分类：{category}\n"
    "发布时间：{publish_time}\n"
    "阅读：{read_count}　点赞：{like_count}　评论：{comment_count}\n"
    "内容类型：{content_type}\n"
    "原文链接：{url}\n"
    "抓取时间：{fetched_at}\n"
    "正文 {text_len} 字\n"
    "{sep}\n\n"
)


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_dirname(title: str, limit: int = DIR_NAME_LIMIT) -> str:
    """把标题转成安全的目录名：去非法字符、压缩空白、超长自动截取。"""
    s = _ILLEGAL.sub(" ", title or "")
    s = _WS.sub(" ", s).strip()
    # Windows 不允许目录名以 . 或空格结尾
    s = s.rstrip(". ")
    if not s:
        s = "未命名"
    if len(s) > limit:
        # 尽量在标点/空格处断句，避免把词截断
        cut = s[:limit]
        for i in range(len(cut) - 1, max(len(cut) - 12, 0), -1):
            if cut[i] in " ，,。！？；、：:·—-":
                cut = cut[:i]
                break
        s = cut.rstrip(". 。，,、；;：:-—")
    return s or "未命名"


def article_dir(article: Dict[str, Any]) -> Path:
    """文章对应的 output 目录（不存在则创建）。"""
    name = safe_dirname(article.get("title") or "")
    target = OUTPUT_DIR / name
    meta_file = target / "meta.json"
    if meta_file.exists():
        try:
            old = json.loads(meta_file.read_text(encoding="utf-8"))
            if int(old.get("article_id") or -1) != int(article["id"]):
                target = OUTPUT_DIR / f"{name}-{article['id']}"
        except Exception:  # noqa: BLE001 - meta 损坏时另建目录
            target = OUTPUT_DIR / f"{name}-{article['id']}"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def export_article(article_id: int) -> Dict[str, Any]:
    """导出一篇文章到 output/<标题>/，返回目录与文件清单。"""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT a.*, c.path AS cache_path, c.summary, c.text_len
               FROM articles a LEFT JOIN raw_cache c ON c.id = a.content_id
               WHERE a.id=?""",
            (article_id,),
        ).fetchone()
    if not row:
        return {"ok": False, "error": "记录不存在"}
    article = dict(row)

    text = ""
    cache_path = Path(article.get("cache_path") or "")
    if cache_path.exists():
        try:
            detail = json.loads(cache_path.read_text(encoding="utf-8"))
            text = detail.get("text") or ""
        except Exception:  # noqa: BLE001
            text = ""
    if not text:
        return {"ok": False, "error": "尚未抓取到原文内容"}

    title = article.get("title") or "(无标题)"
    directory = article_dir(article)
    header = TXT_HEADER.format(
        title=title,
        source=article.get("source") or "-",
        author=article.get("author") or "-",
        category=article.get("category") or article.get("orig_category") or "-",
        publish_time=article.get("publish_time") or "-",
        read_count=article.get("read_count") or 0,
        like_count=article.get("like_count") or 0,
        comment_count=article.get("comment_count") or 0,
        content_type=article.get("content_type") or "文章",
        url=article.get("url") or "-",
        fetched_at=article.get("fetched_at") or "-",
        text_len=len(text),
        sep="=" * 30,
    )
    txt_path = directory / "原文.txt"
    _write_text(txt_path, header + text + "\n")

    meta = {
        "article_id": article_id,
        "title": title,
        "source": article.get("source") or "",
        "author": article.get("author") or "",
        "category": article.get("category") or article.get("orig_category") or "",
        "content_type": article.get("content_type") or "文章",
        "publish_time": article.get("publish_time") or "",
        "read_count": article.get("read_count") or 0,
        "like_count": article.get("like_count") or 0,
        "url": article.get("url") or "",
        "fetched_at": article.get("fetched_at") or "",
        "text_len": len(text),
        "summary": article.get("summary") or "",
        "exported_at": _now(),
    }
    _write_text(directory / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

    rel = directory.relative_to(OUTPUT_DIR).as_posix()
    with write_conn() as conn:
        conn.execute(
            "UPDATE articles SET output_dir=?, updated_at=? WHERE id=?",
            (rel, _now(), article_id),
        )
    return {
        "ok": True,
        "article_id": article_id,
        "dir": rel,
        "path": str(directory),
        "files": ["原文.txt", "meta.json"],
    }


def export_many(article_ids: List[int]) -> Dict[str, Any]:
    items = [export_article(i) for i in article_ids if i]
    ok = [i for i in items if i.get("ok")]
    return {"exported": len(ok), "items": items}


def output_url(rel: Optional[str]) -> str:
    """把 output 下的相对路径转成可访问的 URL。"""
    if not rel:
        return ""
    return "/output/" + str(rel).replace("\\", "/").lstrip("/")
