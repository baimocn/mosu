# -*- coding: utf-8 -*-
"""墨溯 GUI 控制台 —— 本地 Web 界面（mosu gui）

只绑 127.0.0.1（个人工具，不对外），纯标准库，零第三方依赖。

端点：
  GET  /              前端单页（gui/index.html）
  GET  /vendor/...    本地 vendor 资源（Vue3，离线可用）
  GET  /api/status    微信通道 / 通道服务 / bot 配置 状态
  POST /api/chat      {message} → BotService 决策，返回 reply/tries/cost/校验
  GET  /api/log?n=50  消息流水（解析 ~/.mosu/wechat.log 的 [收到]/[回复]/[!]）

设计原则：
  - 活性判断用心跳文件而非计划任务状态（任务"Running"不代表轮询活着）
  - 所有异常都转化成 JSON 错误，前端统一呈现，不允许 500 裸奔
"""
from __future__ import annotations

import json
import os
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GUI_DIR = Path(__file__).resolve().parent
ROOT = GUI_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HEARTBEAT_MAX_AGE = 60   # 秒；超过视为服务不在线


def _heartbeat_age() -> int | None:
    """通道服务最近一次心跳距现在多少秒；没有心跳文件返回 None。"""
    import core.wechat as wx
    hb = wx._read_json(wx.mosu_home() / "wechat.heartbeat")
    ts = hb.get("ts")
    if not ts:
        return None
    return max(0, int(time.time() - ts))


def run_gui(corpus_path, port: int | None = None) -> None:
    from core.bot.service import BotService
    import core.wechat as wx

    port = port or int(os.environ.get("MOSU_GUI_PORT", "8788"))
    bot = BotService(corpus_path)
    n = len(getattr(bot.retriever, "corpus", []))
    print(f"[mosu-gui] 语料 {n} 条，监听 http://127.0.0.1:{port}")

    def build_status() -> dict:
        cred = wx.load_cred()
        sync = wx._read_json(wx.sync_path())
        age = _heartbeat_age()
        return {
            "wechat": {
                "logged_in": bool(cred.get("token")),
                "account": cred.get("account_id", ""),
                "user": cred.get("user_id", ""),
                "base_url": cred.get("base_url", ""),
                "cursor": bool(sync.get("buf")),
            },
            "service": {
                "alive": age is not None and age < HEARTBEAT_MAX_AGE,
                "last_seen": age,
            },
            "bot": {
                "corpus": n,
                "model": os.environ.get("MOSU_BOT_MODEL", "gpt-4o-mini"),
                "base_url": os.environ.get(
                    "MOSU_BOT_BASE_URL", "https://api.openai.com/v1"),
            },
        }

    def read_log(n: int = 50) -> list:
        p = wx.mosu_home() / "wechat.log"
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        events = []
        for ln in lines:
            if len(ln) < 15:
                continue
            ts, msg = ln[:14], ln[15:]
            kind = "log"
            for tag in ("[收到]", "[回复]", "[!]"):
                if msg.startswith(tag):
                    kind = tag
                    msg = msg[len(tag):].strip()
                    break
            events.append({"time": ts, "kind": kind, "text": msg})
        return events

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", code)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                f = GUI_DIR / "index.html"
                if not f.exists():
                    self._json({"error": "gui/index.html 缺失"}, 500)
                    return
                self._send(f.read_bytes(), "text/html; charset=utf-8")
                return
            if path.startswith("/vendor/"):
                # 只允许 vendor 目录内的静态文件，防目录穿越
                rel = path[len("/vendor/"):].strip("/")
                f = (GUI_DIR / "vendor" / rel).resolve()
                if not str(f).startswith(str((GUI_DIR / "vendor").resolve())) \
                        or not f.is_file():
                    self._json({"error": "not found"}, 404)
                    return
                ctype = ("application/javascript"
                         if f.suffix == ".js" else "application/octet-stream")
                self._send(f.read_bytes(), ctype)
                return
            if path == "/api/status":
                try:
                    self._json(build_status())
                except Exception as e:  # noqa: BLE001
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path == "/api/log":
                try:
                    n = 50
                    if "?" in self.path:
                        q = dict(kv.split("=", 1) for kv in
                                 self.path.split("?", 1)[1].split("&") if "=" in kv)
                        n = max(1, min(200, int(q.get("n", 50))))
                    self._json({"events": read_log(n)})
                except Exception as e:  # noqa: BLE001
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            self._json({"error": f"unknown path: {path}"}, 404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path != "/api/chat":
                self._json({"error": "POST /api/chat only"}, 404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                query = (payload.get("message") or "").strip()
                if not query:
                    self._json({"error": "message 不能为空"}, 400)
                    return
                result = bot.chat(query, conv_id="gui")
                self._json(result)
            except SystemExit as e:
                self._json({"error": str(e)}, 500)
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # 端口已被占用 → 认为服务已在跑，直接开浏览器，不再起第二份
    import socket
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            print(f"[mosu-gui] 端口 {port} 已在服务，直接打开浏览器")
            if "--no-browser" not in sys.argv:
                webbrowser.open(f"http://127.0.0.1:{port}")
            return

    print(f"[mosu-gui] 打开浏览器 http://127.0.0.1:{port} （Ctrl+C 停止）")
    try:
        if "--no-browser" not in sys.argv:
            webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
