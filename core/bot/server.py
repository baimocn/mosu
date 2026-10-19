# -*- coding: utf-8 -*-
"""HTTP 服务 —— POST /chat {message, conversation_id?, topk?} / GET /health

另提供 OpenAI 兼容端点 POST /v1/chat/completions，供外部框架（如 OpenClaw）
把墨溯直接当作它的 LLM 后端调用，见 `_openai_chat`。

只绑 127.0.0.1（个人工具，不对外）。
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def run_server(corpus_path: str | Path, port: int | None = None) -> None:
    from core.bot.service import BotService

    port = port or int(os.environ.get("MOSU_BOT_PORT", "8787"))
    bot = BotService(corpus_path)
    n = len(getattr(bot.retriever, "corpus", []))
    print(f"[mosu-bot] 语料 {n} 条，监听 127.0.0.1:{port}")
    print(f"[mosu-bot] 后端 {os.environ.get('MOSU_BOT_BASE_URL', 'https://api.openai.com/v1')}"
          f" 模型 {os.environ.get('MOSU_BOT_MODEL', 'gpt-4o-mini')}")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._json({"ok": True, "corpus": n})
            else:
                self._json({"error": "GET /health only"}, 404)

        def _openai_chat(self):
            """OpenAI 兼容端点：让外部框架（如 OpenClaw）把墨溯当成它的 LLM 后端。

            只取最后一条 user 消息作为输入，**忽略外部传入的 system prompt**——
            风格、约束、校验全部由墨溯自己的 BotService 决定，外部只当通道。
            """
            import time
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                messages = payload.get("messages") or []
                query = ""
                for m in reversed(messages):
                    if (m.get("role") or "") == "user":
                        query = (m.get("content") or "").strip()
                        break
                if not query:
                    self._json({"error": "messages 里没有 user 消息"}, 400)
                    return
                result = bot.chat(query, conv_id="openclaw")
                self._json({
                    "id": "chatcmpl-mosu",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": payload.get("model", "mosu"),
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant",
                                    "content": result.get("reply", "")},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0},
                })
            except SystemExit as e:
                self._json({"error": str(e)}, 500)
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_POST(self):
            if self.path == "/v1/chat/completions":
                self._openai_chat()
                return
            if self.path != "/chat":
                self._json({"error": "POST /chat 或 /v1/chat/completions"}, 404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                query = (payload.get("message") or "").strip()
                if not query:
                    self._json({"error": "message 不能为空"}, 400)
                    return
                result = bot.chat(query, conv_id=payload.get("conversation_id"),
                                  topk=int(payload.get("topk", 5)))
                self._json(result)
            except SystemExit as e:
                self._json({"error": str(e)}, 500)
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
