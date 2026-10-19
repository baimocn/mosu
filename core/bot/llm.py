# -*- coding: utf-8 -*-
"""云端 LLM 客户端（OpenAI 兼容协议，urllib 零依赖）。

可换任意后端：MOSU_BOT_BASE_URL 指向任何 OpenAI 兼容端点
（OpenAI / DeepSeek / Moonshot / 本地 vLLM / ollama 的 OpenAI 兼容层…）。
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path


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

    def chat(self, messages: list[dict], temperature: float = 0.8,
             max_tokens: int = 300) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode("utf-8")
        # 部分网关/代理会按 User-Agent 拦截默认库 UA（如 Python-urllib），
        # 用常规浏览器 UA 规避，对直连（无代理）场景同样无害。
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/120.0 Safari/537.36"},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
