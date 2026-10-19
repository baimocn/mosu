# -*- coding: utf-8 -*-
"""云端大模型配置（URL / API Key / 模型）的读写与连通性测试。

墨溯本身全离线能跑（解密、统计、画像都不需要大模型）。只有两类功能要联网调用：
  1. 数字分身对话（bot）
  2. 密钥获取失败时的 AI 辅助诊断

所以规则是：**需要时才要求配置，且必须由用户亲手填 URL / API Key**——
墨溯不内置任何服务端地址，也不替用户做决定。配置落在 ~/.mosu/config.json。

优先级：进程环境变量 > 配置文件 > 内置默认值（仅 base_url 有默认值）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

_ENV_KEYS = {
    "baseUrl": "MOSU_BOT_BASE_URL",
    "apiKey": "MOSU_BOT_API_KEY",
    "model": "MOSU_BOT_MODEL",
}


def _vault_path() -> Path:
    from core.dataset import KEYVAULT
    return KEYVAULT


def load_llm_config(include_key: bool = False) -> dict:
    """读取生效配置。include_key=False 时不返回 apiKey 明文（给前端展示用）。"""
    from core.dataset import load_keyvault
    file_cfg = load_keyvault().get("llm") or {}
    base = (os.environ.get(_ENV_KEYS["baseUrl"])
            or file_cfg.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/")
    key = os.environ.get(_ENV_KEYS["apiKey"]) or file_cfg.get("apiKey") or ""
    model = (os.environ.get(_ENV_KEYS["model"])
             or file_cfg.get("model") or DEFAULT_MODEL)
    try:
        temp = float(os.environ.get("MOSU_BOT_TEMPERATURE")
                     or file_cfg.get("temperature") or 0.8)
    except (TypeError, ValueError):
        temp = 0.8
    return {
        "baseUrl": base,
        "apiKey": key if include_key else ("***" if key else ""),
        "model": model,
        "temperature": temp,
        "hasKey": bool(key),
        "source": ("env" if any(os.environ.get(v) for v in _ENV_KEYS.values())
                   else ("file" if file_cfg else "default")),
        "savedAt": file_cfg.get("savedAt", ""),
    }


def save_llm_config(base_url: str = "", api_key: str = "", model: str = "",
                    temperature: float | None = None) -> dict:
    """写入配置文件（只写非空字段；api_key 为空串表示不改动）。"""
    from core.dataset import load_keyvault, save_keyvault
    data = load_keyvault()
    cfg = dict(data.get("llm") or {})
    if base_url:
        cfg["baseUrl"] = base_url.strip().rstrip("/")
    if api_key:
        cfg["apiKey"] = api_key.strip()
    if model:
        cfg["model"] = model.strip()
    if temperature is not None:
        cfg["temperature"] = float(temperature)
    cfg["savedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["llm"] = cfg
    save_keyvault(data)
    return load_llm_config()


def clear_llm_config() -> None:
    from core.dataset import load_keyvault, save_keyvault
    data = load_keyvault()
    data.pop("llm", None)
    save_keyvault(data)


def test_connection(base_url: str = "", api_key: str = "", model: str = "",
                    timeout: int = 25) -> dict:
    """真实发一条最小请求验证配置可用（不会被当成"配了其实不通"）。

    返回 {ok, latencyMs, model, error, hint}。失败时给出人能行动的建议。
    """
    eff = load_llm_config(include_key=True)
    base = (base_url or eff["baseUrl"]).rstrip("/")
    key = api_key or eff["apiKey"]
    mdl = model or eff["model"]
    if not key:
        return {"ok": False, "error": "未填写 API Key",
                "hint": "在下方填入你的 API Key 后再测试。墨溯不会内置任何密钥。",
                "stage": "nokey"}
    url = f"{base}/chat/completions"
    body = json.dumps({
        "model": mdl,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8, "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 "User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        txt = ""
        try:
            txt = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            pass
        return {"ok": True, "latencyMs": int((time.time() - t0) * 1000),
                "model": data.get("model", mdl), "reply": (txt or "")[:80],
                "baseUrl": base}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        hint = {
            401: "API Key 不对或已失效（401）。检查 Key 是否复制完整、是否有空格。",
            403: "没有该接口的权限（403）。确认账号余额/权限或换一个模型。",
            404: "地址或模型名不对（404）。baseUrl 一般以 /v1 结尾，模型名要服务端支持。",
            429: "触发限流（429）。稍后再试，或换一个额度充足的 Key。",
        }.get(e.code, f"服务端返回 HTTP {e.code}。")
        return {"ok": False, "error": f"HTTP {e.code}", "detail": detail,
                "hint": hint, "stage": "http", "baseUrl": base}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "hint": "连不上服务端。检查网络、代理，或 baseUrl 是否写错。",
                "stage": "network", "baseUrl": base}
    except Exception as e:                      # 解析失败等
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "hint": "服务端返回了无法解析的内容，确认这是 OpenAI 兼容端点。",
                "stage": "parse", "baseUrl": base}
