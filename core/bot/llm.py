# -*- coding: utf-8 -*-
"""云端 LLM 客户端（OpenAI 兼容协议）。

可换任意后端：MOSU_BOT_BASE_URL 指向任何 OpenAI 兼容端点
（OpenAI / DeepSeek / Moonshot / 本地 vLLM / ollama 的 OpenAI 兼容层…）。

双通道设计：
  - 无代理环境 → http.client 持久连接（省每次 TLS 握手，约 200ms+）
  - 有代理环境（https_proxy/HTTP_PROXY，如 CI/沙箱）→ urllib 通道，
    让代理接管连接（HTTPSConnection 直连会被代理挡）
连接被服务端 keep-alive 超时关掉时自动重建重试一次。
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0 Safari/537.36")


def _load_dotenv() -> None:
    """可选 .env（项目根，gitignore 掉）兜底环境变量，不覆盖已存在的。

    优先级：进程环境变量 > .env 文件 > 代码默认值。
    这样 `mosu bot` 不用每次手动 export，又不污染代码与版本库。
    """
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not env_path.exists():
        return
    with env_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_dotenv()


class LlmClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: int = 60):
        self.base_url = (base_url or os.environ.get("MOSU_BOT_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("MOSU_BOT_API_KEY", "")
        self.model = model or os.environ.get("MOSU_BOT_MODEL", "gpt-4o-mini")
        self.timeout = timeout
        if not self.api_key:
            raise SystemExit("[x] 未配置 MOSU_BOT_API_KEY（云端 LLM 的 key）")
        # 代理环境（沙箱/CI）走 urllib 通道；直连环境才复用连接
        self._proxy = bool(os.environ.get("https_proxy")
                           or os.environ.get("HTTPS_PROXY")
                           or os.environ.get("http_proxy")
                           or os.environ.get("HTTP_PROXY"))
        self._conn: http.client.HTTPSConnection | None = None

    # ---------------------------------------------------------------- 连接
    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _persistent(self) -> http.client.HTTPSConnection | None:
        """无代理 + https 才复用连接；否则返回 None 走 urllib 通道。"""
        if self._proxy:
            return None
        u = urlparse(self.base_url)
        if u.scheme != "https":
            return None
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(
                u.hostname, u.port or 443, timeout=self.timeout)
        return self._conn

    # ---------------------------------------------------------------- 调用
    def _headers(self) -> dict:
        # 部分网关/代理会按 User-Agent 拦截默认库 UA（如 Python-urllib），
        # 用常规浏览器 UA 规避，对直连（无代理）场景同样无害。
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": _UA}

    def _path(self) -> str:
        return urlparse(self.base_url).path.rstrip("/") + "/chat/completions"

    def chat(self, messages: list[dict], temperature: float = 0.8,
             max_tokens: int = 300) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode("utf-8")
        headers = self._headers()

        # 通道 A：持久连接（省 TLS 握手）；断了自动重建重试一次
        conn = self._persistent()
        if conn is not None:
            for attempt in (1, 2):
                try:
                    conn.request("POST", self._path(), body=body, headers=headers)
                    resp = conn.getresponse()
                    data = json.loads(resp.read().decode("utf-8"))
                    return data["choices"][0]["message"]["content"].strip()
                except Exception:
                    self._close_conn()
                    if attempt == 2:
                        raise
                    conn = self._persistent()

        # 通道 B：urllib（代理环境，或 http 明文端点）
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
