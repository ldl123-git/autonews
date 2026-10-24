"""大模型接入配置：服务商预设 + 配置增删改查 + 连通性自检。

设计要点：
- 一条配置 = 一个「大模型接入点」（服务商 + 协议 + 端点 + Key + 模型名）；
- 按用途分类（category）：article=文章改写，video=视频生成，同一服务商可各配一条；
- 列表接口不返回明文 Key，只给掩码；编辑时空 Key 表示「保持原值」；
- 同分类内 is_default 唯一，resolve() 按「默认 → 优先级 → 第一条启用」取用。
"""
import datetime as dt
import time
from typing import Any, Dict, List, Optional

import httpx

from . import settings as _settings
from .db import get_conn, write_conn

# 用途分类
CATEGORIES: List[Dict[str, Any]] = [
    {"key": "article", "title": "文章改写", "desc": "标题 / 正文改写、摘要与文案生成"},
    {"key": "video", "title": "视频生成", "desc": "视频脚本、分镜与画面生成链路"},
]

# 服务商预设：选了自动带出协议、端点与常用模型名
PROVIDERS: List[Dict[str, Any]] = [
    {"key": "deepseek", "label": "DeepSeek", "api_style": "openai",
     "base_url": "https://api.deepseek.com/v1", "models": ["deepseek-chat", "deepseek-reasoner"]},
    {"key": "qwen", "label": "通义千问 DashScope", "api_style": "openai",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "models": ["qwen-max", "qwen-plus", "qwen-turbo", "qwen-long"]},
    {"key": "moonshot", "label": "Moonshot Kimi", "api_style": "openai",
     "base_url": "https://api.moonshot.cn/v1",
     "models": ["moonshot-v1-128k", "moonshot-v1-32k", "kimi-k2-turbo-preview"]},
    {"key": "glm", "label": "智谱 GLM", "api_style": "openai",
     "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "models": ["glm-4-plus", "glm-4-flash", "glm-4-air"]},
    {"key": "doubao", "label": "豆包（火山方舟）", "api_style": "openai",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3",
     "models": ["doubao-pro-32k", "doubao-lite-32k"]},
    {"key": "openai", "label": "OpenAI", "api_style": "openai",
     "base_url": "https://api.openai.com/v1", "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"]},
    {"key": "claude", "label": "Anthropic Claude", "api_style": "anthropic",
     "base_url": "https://api.anthropic.com/v1",
     "models": ["claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"]},
    {"key": "gemini", "label": "Google Gemini", "api_style": "gemini",
     "base_url": "https://generativelanguage.googleapis.com/v1beta",
     "models": ["gemini-2.5-flash", "gemini-2.5-pro"]},
    {"key": "ollama", "label": "Ollama 本地", "api_style": "openai",
     "base_url": "http://127.0.0.1:11434/v1", "models": ["qwen2.5:7b", "llama3.1:8b"], "no_key": True},
    {"key": "custom", "label": "自定义（OpenAI 兼容）", "api_style": "openai", "base_url": "", "models": []},
]
PROVIDER_MAP = {p["key"]: p for p in PROVIDERS}

# 请求协议
API_STYLES = [
    {"key": "openai", "label": "OpenAI 兼容（/chat/completions）"},
    {"key": "anthropic", "label": "Anthropic Messages（/messages）"},
    {"key": "gemini", "label": "Google Gemini（/models:generateContent）"},
]

EDITABLE = {"name", "category", "provider", "api_style", "base_url", "api_key", "model",
            "temperature", "top_p", "max_tokens", "timeout", "priority", "enabled",
            "is_default", "system_prompt", "note"}
CATEGORY_KEYS = {c["key"] for c in CATEGORIES}


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _provider_defaults(provider: str) -> Dict[str, Any]:
    p = PROVIDER_MAP.get(provider) or PROVIDER_MAP["custom"]
    return {"api_style": p["api_style"], "base_url": p["base_url"], "no_key": bool(p.get("no_key"))}


# ---------------------------------------------------------------- 查询
def list_configs(category: str = "") -> List[Dict[str, Any]]:
    sql = "SELECT * FROM llm_configs"
    args: List[Any] = []
    if category:
        sql += " WHERE category=?"
        args.append(category)
    sql += " ORDER BY category, is_default DESC, priority DESC, id DESC"
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    return [_present(r) for r in rows]


def get_config(config_id: int, mask: bool = True) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM llm_configs WHERE id=?", (config_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    return _present(d) if mask else d


def resolve(category: str = "article", prefer_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """取一个可用的模型配置（含明文 Key，供调用层使用）。

    顺序：指定 id → 该分类默认 → 优先级最高 → 第一条启用。
    """
    if prefer_id:
        row = get_config(prefer_id, mask=False)
        if row:
            return row
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM llm_configs WHERE category=? AND enabled=1"
            " ORDER BY is_default DESC, priority DESC, id DESC LIMIT 1",
            (category,),
        ).fetchone()
    return dict(row) if row else None


def meta() -> Dict[str, Any]:
    return {"categories": CATEGORIES, "providers": PROVIDERS, "api_styles": API_STYLES}


# ---------------------------------------------------------------- 写入
def upsert(payload: Dict[str, Any], config_id: Optional[int] = None) -> int:
    data = {k: v for k, v in (payload or {}).items() if k in EDITABLE}
    # 新建必须有名字；编辑时只校验显式传来的空名字
    if config_id is None or "name" in data:
        if not str(data.get("name") or "").strip():
            raise ValueError("请填写配置名称")
    category = str(data.get("category") or "article")
    if category not in CATEGORY_KEYS:
        raise ValueError("未知的用途分类")

    now = _now()
    want_default = bool(data.get("is_default"))
    with write_conn() as conn:
        if config_id:
            old = conn.execute("SELECT * FROM llm_configs WHERE id=?", (config_id,)).fetchone()
            if not old:
                raise LookupError("配置不存在")
            category = str(data.get("category") or old["category"])
            updates: Dict[str, Any] = {}
            for k, v in data.items():
                if k == "api_key":
                    continue  # 空值不覆盖已有 Key
                updates[k] = v
            if "api_key" in data and str(data["api_key"]).strip():
                updates["api_key"] = str(data["api_key"]).strip()
            updates["updated_at"] = now
            sets = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE llm_configs SET {sets} WHERE id=?",
                         [_scalar(v) for v in updates.values()] + [config_id])
        else:
            preset = _provider_defaults(str(data.get("provider") or "custom"))
            values = {
                "name": str(data.get("name")).strip(),
                "category": category,
                "provider": str(data.get("provider") or "custom"),
                "api_style": str(data.get("api_style") or preset["api_style"]),
                "base_url": str(data.get("base_url") or preset["base_url"]),
                "api_key": str(data.get("api_key") or "").strip(),
                "model": str(data.get("model") or ""),
                "temperature": _num(data.get("temperature"), 0.7),
                "top_p": _num(data.get("top_p"), 1),
                "max_tokens": int(_num(data.get("max_tokens"), 4096)),
                "timeout": int(_num(data.get("timeout"), 90)),
                "priority": int(_num(data.get("priority"), 0)),
                "enabled": 1 if data.get("enabled", True) else 0,
                "is_default": 1 if data.get("is_default") else 0,
                "system_prompt": str(data.get("system_prompt") or ""),
                "note": str(data.get("note") or ""),
                "created_at": now,
                "updated_at": now,
            }
            cols = ", ".join(values)
            cur = conn.execute(
                f"INSERT INTO llm_configs ({cols}) VALUES ({','.join('?' * len(values))})",
                list(values.values()),
            )
            config_id = int(cur.lastrowid or 0)
        if want_default:
            conn.execute("UPDATE llm_configs SET is_default=0 WHERE category=? AND id<>?",
                         [category, config_id])
        else:
            row = conn.execute("SELECT category, enabled FROM llm_configs WHERE id=?",
                               (config_id,)).fetchone()
            # 该分类下还没有默认项时，自动把当前这条设为默认
            has_default = conn.execute(
                "SELECT COUNT(*) c FROM llm_configs WHERE category=? AND is_default=1",
                (row["category"],)).fetchone()["c"]
            if not has_default and row["enabled"]:
                conn.execute("UPDATE llm_configs SET is_default=1 WHERE id=?", (config_id,))
    return int(config_id)


def set_default(config_id: int) -> None:
    with write_conn() as conn:
        row = conn.execute("SELECT category FROM llm_configs WHERE id=?", (config_id,)).fetchone()
        if not row:
            raise LookupError("配置不存在")
        conn.execute("UPDATE llm_configs SET is_default=0 WHERE category=?", (row["category"],))
        conn.execute("UPDATE llm_configs SET is_default=1, updated_at=? WHERE id=?",
                     [_now(), config_id])


def delete(config_id: int) -> int:
    with write_conn() as conn:
        row = conn.execute("SELECT category, is_default FROM llm_configs WHERE id=?",
                           (config_id,)).fetchone()
        if not row:
            return 0
        conn.execute("DELETE FROM llm_configs WHERE id=?", (config_id,))
        if row["is_default"]:
            nxt = conn.execute(
                "SELECT id FROM llm_configs WHERE category=? AND enabled=1"
                " ORDER BY priority DESC, id DESC LIMIT 1", (row["category"],)).fetchone()
            if nxt:
                conn.execute("UPDATE llm_configs SET is_default=1 WHERE id=?", (nxt["id"],))
    return 1


# ---------------------------------------------------------------- 连通性自检
def test_config(config_id: Optional[int] = None, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """测试一条配置能否连通。已保存的传 id，未保存的直接传表单内容。"""
    if config_id:
        cfg = get_config(config_id, mask=False)
        if not cfg:
            raise LookupError("配置不存在")
        if payload:
            for k in ("base_url", "model", "api_key", "api_style", "provider", "timeout"):
                if payload.get(k):
                    cfg[k] = payload[k]
    else:
        cfg = dict(payload or {})
        preset = _provider_defaults(str(cfg.get("provider") or "custom"))
        cfg["api_style"] = cfg.get("api_style") or preset["api_style"]
        cfg["base_url"] = cfg.get("base_url") or preset["base_url"]

    style = str(cfg.get("api_style") or "openai")
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    model = str(cfg.get("model") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    timeout = max(5.0, float(cfg.get("timeout") or 30))
    if not base:
        return {"ok": False, "message": "接口地址为空", "latency_ms": 0}
    if not model:
        return {"ok": False, "message": "模型名为空", "latency_ms": 0}

    started = time.time()
    try:
        if style == "anthropic":
            reply = _test_anthropic(base, api_key, model, timeout)
        elif style == "gemini":
            reply = _test_gemini(base, api_key, model, timeout)
        else:
            reply = _test_openai(base, api_key, model, timeout)
    except httpx.HTTPStatusError as exc:  # 4xx/5xx 带正文，便于定位
        body = (exc.response.text or "")[:300]
        return {"ok": False, "latency_ms": _ms(started),
                "message": f"HTTP {exc.response.status_code}：{body or exc.response.reason_phrase}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "latency_ms": _ms(started), "message": f"{exc.__class__.__name__}：{exc}"}
    return {"ok": True, "latency_ms": _ms(started), "message": f"连通正常 · {model}", "reply": reply}


# ---------------------------------------------------------------- 统一对话调用
async def achat(cfg: Dict[str, Any], prompt: str, system: str = "") -> str:
    """按配置的协议发一次对话，返回模型输出的正文。"""
    style = str(cfg.get("api_style") or "openai")
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    model = str(cfg.get("model") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    timeout = max(5.0, float(cfg.get("timeout") or 90))
    max_tokens = int(cfg.get("max_tokens") or 4096)
    temperature = float(cfg.get("temperature") or 0.7)
    if not base:
        raise ValueError("接口地址为空")
    if not model:
        raise ValueError("模型名为空")

    async with httpx.AsyncClient(timeout=timeout) as client:
        if style == "anthropic":
            resp = await client.post(
                base if base.endswith("/messages") else f"{base}/messages",
                headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01",
                         **({"x-api-key": api_key} if api_key else {})},
                json={"model": model, "max_tokens": max_tokens, "temperature": temperature,
                      **({"system": system} if system else {}),
                      "messages": [{"role": "user", "content": prompt}]},
            )
        elif style == "gemini":
            resp = await client.post(
                f"{base}/models/{model}:generateContent",
                params={"key": api_key} if api_key else None,
                json={"contents": [{"parts": [{"text": (system + "\n\n" if system else "") + prompt}]}],
                      "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature}},
            )
        else:
            resp = await client.post(
                base if base.endswith("/chat/completions") else f"{base}/chat/completions",
                headers={"Content-Type": "application/json",
                         **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
                json={"model": model, "temperature": temperature, "max_tokens": max_tokens,
                      "messages": ([{"role": "system", "content": system}] if system else [])
                      + [{"role": "user", "content": prompt}]},
            )
    resp.raise_for_status()
    return _extract_text(resp.json(), style)


def _extract_text(data: Dict[str, Any], style: str) -> str:
    try:
        if style == "anthropic":
            return "".join(b.get("text", "") for b in data.get("content") or [])
        if style == "gemini":
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"返回结构无法解析：{str(data)[:200]}") from exc


def _test_openai(base: str, api_key: str, model: str, timeout: float) -> str:
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "max_tokens": 16, "temperature": 0,
               "messages": [{"role": "user", "content": "只回复 ok 两个字母"}]}
    resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    try:
        return str(data["choices"][0]["message"]["content"])[:60]
    except (KeyError, IndexError, TypeError):
        return "HTTP 200"


def _test_anthropic(base: str, api_key: str, model: str, timeout: float) -> str:
    url = base if base.endswith("/messages") else f"{base}/messages"
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if api_key:
        headers["x-api-key"] = api_key
    payload = {"model": model, "max_tokens": 16,
               "messages": [{"role": "user", "content": "只回复 ok 两个字母"}]}
    resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    try:
        return str(data["content"][0]["text"])[:60]
    except (KeyError, IndexError, TypeError):
        return "HTTP 200"


def _test_gemini(base: str, api_key: str, model: str, timeout: float) -> str:
    url = f"{base}/models/{model}"
    if api_key:
        url += f"?key={api_key}"
    resp = httpx.get(url, timeout=timeout)
    resp.raise_for_status()
    return f"模型 {model} 可用"


def _ms(started: float) -> int:
    return int((time.time() - started) * 1000)


# ---------------------------------------------------------------- 内部工具
def _present(row: Dict[str, Any]) -> Dict[str, Any]:
    """对外输出：Key 只给掩码。"""
    out = dict(row)
    key = out.get("api_key") or ""
    out["api_key_masked"] = _settings.mask_secret(key)
    out["has_api_key"] = bool(key)
    out["api_key"] = ""
    out["enabled"] = int(out.get("enabled") or 0)
    out["is_default"] = int(out.get("is_default") or 0)
    return out


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return 1 if value else 0
    return value