# -*- coding: utf-8 -*-
"""云端 LLM 客户端（OpenAI 兼容协议，urllib 零依赖）。

可换任意后端：MOSU_BOT_BASE_URL 指向任何 OpenAI 兼容端点
（OpenAI / DeepSeek / Moonshot / 本地 vLLM / ollama 的 OpenAI 兼容层…）。
"""
from __future__ import annotations

import json
import os
import urllib.request


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
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
