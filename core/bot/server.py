# -*- coding: utf-8 -*-
"""HTTP 服务 —— POST /chat {message, conversation_id?, topk?} / GET /health

只绑 127.0.0.1（个人工具，不对外）。
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
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

        def do_POST(self):
            if self.path != "/chat":
                self._json({"error": "POST /chat only"}, 404)
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

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
