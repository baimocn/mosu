# -*- coding: utf-8 -*-
"""墨溯 GUI 控制台 —— 本地 Web 界面（mosu gui）

只绑 127.0.0.1（个人工具，不对外），纯标准库，零第三方依赖。

端点：
  GET  /                 前端单页（gui/index.html）
  GET  /vendor/...       本地 vendor 资源（Vue3，离线可用）
  GET  /api/status       微信通道 / 通道服务 / bot / 数据源 状态
  POST /api/chat         {message} → BotService 决策，返回 reply/tries/cost/校验
  GET  /api/log?n=50     消息流水（解析 ~/.mosu/wechat.log 的 [收到]/[回复]/[!]）
  GET  /api/sessions     会话列表（type/q 过滤，offset 翻页）
  GET  /api/messages     某会话消息（session&limit&offset）
  GET  /api/search?q     全库搜索
  GET  /api/media?md5    解密图片二进制（浏览器 <img> 直接渲染）

设计原则：
  - Dataset/MediaStore 懒加载：GUI 启动不等解密，首次数据请求才付 4~10s
  - 活性判断用心跳文件而非计划任务状态（任务"Running"不代表轮询活着）
  - 所有异常都转化成 JSON 错误，前端统一呈现，不允许 500 裸奔
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

GUI_DIR = Path(__file__).resolve().parent
ROOT = GUI_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HEARTBEAT_MAX_AGE = 60   # 秒；超过视为服务不在线

_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
         "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}


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

    # ---- 懒加载：解密很慢（4~10s），绝不能拖慢 GUI 启动 ----
    _ds = None
    _media = None
    _ds_err = None
    _ds_lock = threading.RLock()  # SQLite 连接跨线程复用：整个数据 API 期间持锁
                                  # （RLock：api_* 持锁时内部 get_ds 可重入），
                                  # 同时防并发首次触发重复解密

    def get_ds():
        nonlocal _ds, _ds_err
        with _ds_lock:
            if _ds is None:
                if _ds_err:
                    raise RuntimeError(_ds_err)
                try:
                    from core.dataset import Dataset
                    _ds = Dataset(quiet=True)
                except SystemExit as e:
                    _ds_err = str(e)
                    raise RuntimeError(_ds_err) from e
            return _ds

    def get_media():
        nonlocal _media
        with _ds_lock:
            if _media is None:
                from core.media import MediaStore
                _media = MediaStore(get_ds().user_dir, ROOT / ".cache").build()
            return _media

    # ---------------------------------------------------------------- 状态
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
            "data": {
                "ready": _ds is not None,
                "error": _ds_err,
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

    # ---------------------------------------------------------------- 数据浏览
    def api_sessions(q) -> dict:
        with _ds_lock:            # 查询全程持锁：SQLite 连接不跨线程并发用
            ss = get_ds().sessions()
            t = q.get("type")
            if t:
                ss = [s for s in ss if s["type"] == t]
            kw = (q.get("q") or "").strip().lower()
            if kw:
                ss = [s for s in ss if kw in (s["displayName"] or "").lower()]
            total = len(ss)
            offset = max(0, int(q.get("offset", 0)))
            limit = max(1, min(int(q.get("limit", 100)), 500))
            page = ss[offset:offset + limit]
            return {
                "total": total, "hasMore": total > offset + len(page),
                "sessions": [{
                    "sessionId": s["sessionId"],
                    "displayName": s["displayName"],
                    "type": s["type"],
                    "messageCount": s["messageCount"],
                    "lastTime": s["lastTime"],
                    "lastMessage": (s["lastMessage"] or "")[:40],
                } for s in page],
            }

    def api_messages(q) -> dict:
        sess = (q.get("session") or "").strip()
        if not sess:
            raise ValueError("缺少 session 参数（会话 wxid）")
        limit = max(1, min(int(q.get("limit", 50)), 200))
        offset = max(0, int(q.get("offset", 0)))
        with _ds_lock:
            r = get_ds().messages(sess, limit=limit, offset=offset)
        return {
            "session": r["session"],
            "count": len(r["messages"]),
            "messages": [{
                "time": m["createTime"],
                "direction": m["direction"],
                "sender": m.get("senderName") or "",
                "kind": m["kind"],
                "content": m.get("content") or "",
                "imgMd5": m.get("imgMd5") or "",
                "voiceMs": m.get("voiceMs"),
                "voiceText": m.get("voiceText") or "",
                "title": m.get("title") or m.get("fileName") or "",
            } for m in r["messages"]],
        }

    def api_search(q) -> dict:
        kw = (q.get("q") or "").strip()
        if not kw:
            return {"total": 0, "messages": []}
        limit = max(1, min(int(q.get("limit", 50)), 200))
        with _ds_lock:
            r = get_ds().search(kw, limit=limit)
        return {
            "total": r["total"],
            "messages": [{
                "time": m["createTime"],
                "session": m.get("sessionName") or "",
                "sessionId": m.get("sessionId") or "",
                "direction": m["direction"],
                "content": (m.get("content") or "")[:120],
            } for m in r["messages"]],
        }

    def api_media(q) -> tuple[bytes, str]:
        md5 = (q.get("md5") or "").strip()
        if len(md5) != 32:
            raise ValueError("md5 必须是 32 位")
        with _ds_lock:
            pair = get_media().get_image(md5)
        if not pair:
            raise ValueError(f"本地找不到该图片（md5={md5}），可能已被清理或没下载")
        data, ext = pair
        if len(data) > 900_000:      # 大图先缩到 1024 宽，别把浏览器撑爆
            try:
                from core.exporter import ChatExporter
                data = ChatExporter._maybe_resize(data, ext, max_width=1024)
            except Exception:
                pass
        return data, _MIME.get(ext.lstrip(".").lower(), "image/jpeg")

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

        def _params(self) -> dict:
            return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

        def do_GET(self):
            path = urlparse(self.path).path
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
                    q = self._params()
                    if "n" in q:
                        n = max(1, min(int(q["n"]), 200))
                    self._json({"events": read_log(n)})
                except Exception as e:  # noqa: BLE001
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path == "/api/sessions":
                try:
                    self._json(api_sessions(self._params()))
                except Exception as e:  # noqa: BLE001
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path == "/api/messages":
                try:
                    self._json(api_messages(self._params()))
                except Exception as e:  # noqa: BLE001
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path == "/api/search":
                try:
                    self._json(api_search(self._params()))
                except Exception as e:  # noqa: BLE001
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path == "/api/media":
                try:
                    data, mime = api_media(self._params())
                    self._send(data, mime)
                except ValueError as e:
                    self._json({"error": str(e)}, 404)
                except Exception as e:  # noqa: BLE001
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            self._json({"error": f"unknown path: {path}"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
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
