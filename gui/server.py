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

import io
import json
import os
import queue
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

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


# ---------------------------------------------------------------- 导出产物目录
EXPORT_ROOT = ROOT / ".cache" / "gui_export"


class _TaskHub:
    """异步任务注册表（关键词统计 / HTML 导出共用）。

    - 单 worker 串行执行：重操作不并发互相踩（它们都要抢 _ds_lock，
      并发只会排队，不如显式串行 + UI 显示排队/耗时）
    - 锁与 _ds_lock 分开：worker 内部要取 _ds_lock，不能拿同一把锁等待
    - 产物目录 EXPORT_ROOT/<task_id>/，历史上限 keep 条，超限淘汰最旧已完成
    """

    def __init__(self, artifact_root: Path, keep: int = 20):
        self.root = artifact_root
        self.keep = keep
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._worker = threading.Thread(target=self._loop, daemon=True,
                                        name="mosu-gui-task")
        self._worker.start()

    def submit(self, kind: str, fn) -> str:
        tid = uuid.uuid4().hex[:12]
        with self._lock:
            self._tasks[tid] = {"id": tid, "kind": kind, "status": "queued",
                                "cost": None, "result": None, "error": None}
            done = [t for t in self._tasks.values()
                    if t["status"] in ("done", "error")]
            for t in done[:max(0, len(done) - self.keep + 1)]:
                self._tasks.pop(t["id"], None)
        self._queue.put((tid, fn))
        return tid

    def _loop(self):
        while True:
            tid, fn = self._queue.get()
            with self._lock:
                t = self._tasks.get(tid)
                if not t:
                    continue
                t["status"] = "running"
            t0 = time.time()
            try:
                result = fn(tid)
                with self._lock:
                    t["status"] = "done"
                    t["result"] = result
            except Exception as e:  # noqa: BLE001 —— 任务失败必须落状态，不能崩 worker
                with self._lock:
                    t["status"] = "error"
                    t["error"] = f"{type(e).__name__}: {e}"
            finally:
                with self._lock:
                    t["cost"] = round(time.time() - t0, 2)

    def fetch(self, tid: str) -> dict | None:
        with self._lock:
            t = self._tasks.get(tid)
            return dict(t) if t else None


def _text_export_bytes(ds, sess: dict, fmt: str,
                       from_ts=None, to_ts=None) -> tuple[bytes, str]:
    """文本格式导出为字节（json/csv/txt）——格式与 CLI `mosu export` 完全一致：
    CSV 表头「时间,方向,发送者,类型,内容」+ utf-8-sig BOM（Excel 友好）；
    TXT 行 `[YYYY-MM-DD HH:MM:SS] 我|对方: 内容`，媒体占位规则同 CLI。
    返回 (bytes, 扩展名)。
    """
    from core.formatters import fmt_time
    r = ds.messages(sess, limit=10 ** 9, order="asc", from_ts=from_ts, to_ts=to_ts)
    msgs = r["messages"]
    if fmt == "json":
        return json.dumps(r, ensure_ascii=False, indent=2).encode("utf-8"), "json"
    if fmt == "csv":
        import csv as _csv
        import io
        buf = io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["时间", "方向", "发送者", "类型", "内容"])
        for m in msgs:
            w.writerow([fmt_time(m["createTime"]),
                        "out" if m["direction"] == "out" else "in",
                        m["senderName"] or "", m["kind"],
                        (m["content"] or "").replace("\n", " ")])
        return buf.getvalue().encode("utf-8-sig"), "csv"
    if fmt != "txt":
        raise ValueError(f"不支持的格式：{fmt}（只支持 json/csv/txt）")
    # txt
    lines = []
    for m in msgs:
        who = "我" if m["direction"] == "out" else (m["senderName"] or "对方")
        c = m["content"] or ""
        if m["kind"] == "image":
            c = "[图片]"
        elif m["kind"] == "voice":
            c = f"[语音 {m['voiceMs']}ms] {m['voiceText'] or ''}"
        elif m["kind"] == "video":
            c = "[视频]"
        elif m["kind"] == "file":
            c = f"[文件] {m['title'] or m['fileName'] or ''}"
        lines.append(f"[{fmt_time(m['createTime'])}] {who}: {c}")
    return ("\n".join(lines) + "\n").encode("utf-8"), "txt"


def _zip_dir(src_dir: Path) -> bytes:
    """把目录打包成 zip 字节（标准库 zipfile）。arcname 相对化，跳过含 ..
    的条目（防穿越进压缩包）。"""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_dir.rglob("*")):
            if not p.is_file():
                continue
            arc = p.relative_to(src_dir).as_posix()
            if ".." in arc.split("/"):
                continue
            zf.write(p, arc)
    return buf.getvalue()


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
    _moments_rows = None      # 朋友圈全量缓存（get_moments 首屏解析后填充）
    _moments_unsup = None     # 无 sns.db 时 True（缓存）
    _ds_lock = threading.RLock()  # SQLite 连接跨线程复用：整个数据 API 期间持锁
                                  # （RLock：api_* 持锁时内部 get_ds 可重入），
                                  # 同时防并发首次触发重复解密
    tasks = _TaskHub(EXPORT_ROOT)   # 异步任务（关键词统计/导出）

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
        # ready 判定看「配置是否就绪」而不是「数据层是否已初始化」：
        # _ds 是懒加载的，进程刚启动时必然是 None，用它判定会让
        # 每次重启都误弹首启向导。配置链（密钥+目录）才是不变的真相。
        try:
            from core.dataset import find_user_dir, load_accounts, load_key_hex
            _accounts, _cur = load_accounts()
            key_ok = bool(load_key_hex(_cur))
            dir_ok = False
            try:
                dir_ok = find_user_dir(None, _cur).exists() if _cur else False
            except Exception:
                dir_ok = False
            data_ready = key_ok and dir_ok
            if not key_ok:
                data_err = "未配置数据库密钥"
            elif not dir_ok:
                data_err = "数据目录不存在"
            else:
                data_err = _ds_err
        except Exception as e:                       # noqa: BLE001
            data_ready = False
            data_err = f"{type(e).__name__}: {e}"
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
                "ready": data_ready if _ds is None else True,
                "error": data_err,
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
        type_ = (q.get("type") or "").strip().lower() or None
        direction = (q.get("direction") or "").strip().lower() or None
        if direction and direction not in ("in", "out"):
            raise ValueError("direction 只支持 in/out")
        from core.dataset import parse_time
        with _ds_lock:
            r = get_ds().messages(sess, limit=limit, offset=offset,
                                  type_=type_, direction=direction,
                                  from_ts=parse_time(q.get("from")),
                                  to_ts=parse_time(q.get("to")))
        # 统一前端契约：id / createTime / senderName / senderId 必备，
        # 并给出分页元数据（hasMore），前端才能做"向上加载更多"。
        # 带筛选时总数不可知（总数指全会话），此时 total=null 由前端只信 hasMore。
        filtered = bool(type_ or direction or q.get("from") or q.get("to"))
        sess_meta = r["session"] or {}
        return {
            "session": sess_meta,
            "count": len(r["messages"]),
            "limit": limit,
            "offset": offset,
            "hasMore": len(r["messages"]) >= limit,
            "total": None if filtered else sess_meta.get("messageCount"),
            "messages": [{
                "id": f"{sess_meta.get('sessionId','')}:{m['createTime']}:{i}",
                "createTime": m["createTime"],
                "direction": m["direction"],
                "senderName": m.get("senderName") or "",
                "senderId": m.get("senderId") or m.get("senderName") or "",
                "kind": m["kind"],
                "content": m.get("content") or "",
                "imgMd5": m.get("imgMd5") or "",
                "voiceMs": m.get("voiceMs"),
                "voiceText": m.get("voiceText") or "",
                "title": m.get("title") or m.get("fileName") or "",
                "fileName": m.get("fileName") or "",
            } for i, m in enumerate(r["messages"])],
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
                "createTime": m["createTime"],
                "sessionName": m.get("sessionName") or "",
                "sessionId": m.get("sessionId") or "",
                "direction": m["direction"],
                "content": (m.get("content") or "")[:200],
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

    def get_moments():
        """朋友圈：首屏全量解析后缓存 rows，翻页直接切片（sns.db 静态假设，
        GUI 会话内不失效）。"""
        nonlocal _moments_rows
        with _ds_lock:
            if _moments_rows is None:
                from core.sns import Moments
                _moments_rows = Moments(get_ds()).entries(limit=10 ** 9)["entries"]
            return _moments_rows

    def api_moments(q) -> dict:
        limit = max(1, min(int(q.get("limit", 30)), 50))
        offset = max(0, int(q.get("offset", 0)))
        user = (q.get("user") or "").strip() or None
        with _ds_lock:
            rows = get_moments()
            if user:
                rows = [e for e in rows if user in (e["author"]["wxid"],)]
            total = len(rows)
            page = rows[offset:offset + limit]
        return {
            "total": total, "offset": offset, "limit": limit,
            "hasMore": total > offset + len(page),
            "entries": page,
            "unsupported": _moments_unsupported(),
        }

    def _moments_unsupported() -> bool:
        nonlocal _moments_unsup
        with _ds_lock:
            if _moments_unsup is None:
                _moments_unsup = not get_ds().has("sns/sns.db")
            return _moments_unsup

    # ---------------------------------------------------------------- 统计
    def api_stats_global() -> dict:
        from core.analytics import Analytics
        with _ds_lock:
            return Analytics(get_ds()).global_stats()

    def api_stats_contacts(q) -> dict:
        from core.analytics import Analytics
        top = max(1, min(int(q.get("top", 20)), 100))
        with _ds_lock:
            return Analytics(get_ds()).contact_stats(top=top)

    def api_stats_time(q) -> dict:
        from core.analytics import Analytics
        by = (q.get("by") or "hour").lower()
        if by not in ("hour", "weekday", "month", "date"):
            raise ValueError("by 只支持 hour/weekday/month/date")
        with _ds_lock:
            return Analytics(get_ds()).time_stats(by=by)

    def _kw_task(top: int, max_msgs: int):
        """TaskHub worker 里跑：全库逐条解压抽关键词（重操作，持锁数十秒）。"""
        def run(tid):
            from core.analytics import Analytics
            with _ds_lock:
                return Analytics(get_ds()).keyword_stats(top=top, max_msgs=max_msgs)
        return run

    def api_stats_keywords(payload: dict) -> dict:
        top = int(payload.get("top") or 30)
        max_msgs = int(payload.get("max_msgs") or 20000)
        if not (1 <= top <= 100):
            raise ValueError("top 需在 1~100")
        if not (1000 <= max_msgs <= 200000):
            raise ValueError("max_msgs 需在 1000~200000")
        tid = tasks.submit("keywords", _kw_task(top, max_msgs))
        return {"taskId": tid, "status": "running"}

    def api_task(q) -> dict:
        tid = (q.get("id") or "").strip()
        t = tasks.fetch(tid)
        if not t:
            raise KeyError(tid)
        return t

    # ---------------------------------------------------------------- 导出
    def _export_text_task(sess_id: str, fmt: str, from_ts, to_ts):
        """TaskHub worker：文本格式（秒级，但走任务统一轮询，前端一套逻辑）。"""
        def run(tid):
            with _ds_lock:
                ds = get_ds()
                sess = ds.find_session(sess_id)
                if not sess:
                    raise LookupError(f"没找到会话「{sess_id}」")
                data, ext = _text_export_bytes(ds, sess, fmt, from_ts, to_ts)
                return {"session": sess["displayName"], "count": len(data),
                        "ext": ext, "data": data.hex()}
        return run

    def _export_html_task(sess_id: str, from_ts, to_ts):
        """TaskHub worker：HTML 整会话导出 + zip（复刻 CLI cmd_export 的 html 分支）。
        语音不带 mp3（GUI 零 Node 原则），HTML 内以转文字呈现。"""
        def run(tid):
            with _ds_lock:
                ds = get_ds()
                sess = ds.find_session(sess_id)
                if not sess:
                    raise LookupError(f"没找到会话「{sess_id}」")
                from core.media import MediaStore
                from core.exporter import ChatExporter
                out_dir = EXPORT_ROOT / tid
                out_dir.mkdir(parents=True, exist_ok=True)
                media = MediaStore(ds.user_dir, ROOT / ".cache").build()
                rows, names, self_id = ds.collect_session_rows(sess, from_ts, to_ts)
                exp = ChatExporter(media, out_dir, voice_map={})
                exp.export(rows, names, self_id, title=sess["displayName"])
                zip_bytes = _zip_dir(out_dir)
                return {"session": sess["displayName"], "count": len(rows),
                        "bytes": len(zip_bytes),
                        "zip": zip_bytes.hex()}
        return run

    def _safe_dirname(s: str) -> str:
        """会话名转安全的目录名（防路径穿越：../ 或 Windows 特殊字符）。"""
        import re as _re
        bad = '<>:"/\\|?*'
        s = _re.sub(r'[\x00-\x1f]', '', s)
        s = "".join(ch for ch in s if ch not in bad).strip().rstrip(".")
        return s[:60] or "chat"

    def _export_all_task(top_n: int, from_ts, to_ts):
        """TaskHub worker：多会话批量导出（每会话一个子目录，打成一个 zip）。
        top_n 限制会话数——全库 190 个会话逐个解密媒体要跑很久，
        GUI 给「前 N 大会话」；真要全量用 CLI `mosu export --all`。"""
        def run(tid):
            with _ds_lock:
                ds = get_ds()
                from core.media import MediaStore
                from core.exporter import ChatExporter
                media = MediaStore(ds.user_dir, ROOT / ".cache").build()
                sessions = sorted(ds.sessions(),
                                  key=lambda s: -(s["messageCount"] or 0))[:top_n]
                out_dir = EXPORT_ROOT / tid
                out_dir.mkdir(parents=True, exist_ok=True)
                done = []
                for sess in sessions:
                    sub = out_dir / _safe_dirname(sess["displayName"])
                    sub.mkdir(parents=True, exist_ok=True)
                    rows, names, self_id = ds.collect_session_rows(sess, from_ts, to_ts)
                    if not rows:
                        continue
                    exp = ChatExporter(media, sub, voice_map={})
                    exp.export(rows, names, self_id, title=sess["displayName"])
                    done.append({"session": sess["displayName"], "count": len(rows)})
                zip_bytes = _zip_dir(out_dir)
                return {"session": f"{len(done)} 个会话", "count": sum(d["count"] for d in done),
                        "bytes": len(zip_bytes), "sessions": done,
                        "zip": zip_bytes.hex()}
        return run

    def api_export(payload: dict):
        scope = (payload.get("scope") or "single").strip()
        if scope == "all":
            top_n = int(payload.get("topN") or 10)
            if not (1 <= top_n <= 30):
                raise ValueError("topN 需在 1~30（全库 190 个会话逐个解密媒体很慢）")
            from core.dataset import parse_time
            try:
                from_ts = parse_time(payload.get("from"))
                to_ts = parse_time(payload.get("to"))
            except SystemExit as e:
                raise ValueError(str(e)) from e
            tid = tasks.submit("export-html", _export_all_task(top_n, from_ts, to_ts))
            return {"taskId": tid, "status": "running",
                    "session": f"前 {top_n} 大会话"}
        sess_id = (payload.get("session") or "").strip()
        fmt = (payload.get("format") or "").lower()
        if not sess_id:
            raise ValueError("缺少 session（会话 wxid）")
        if fmt not in ("json", "csv", "txt", "html"):
            raise ValueError("format 只支持 json/csv/txt/html")
        from core.dataset import parse_time
        try:
            from_ts = parse_time(payload.get("from"))
            to_ts = parse_time(payload.get("to"))
        except SystemExit as e:
            raise ValueError(str(e)) from e
        # 先校验会话存在（同步路径直接 404；异步路径提前失败）
        with _ds_lock:
            sess = get_ds().find_session(sess_id)
            if not sess:
                raise LookupError(f"没找到会话「{sess_id}」")
        if fmt == "html":
            tid = tasks.submit("export-html",
                               _export_html_task(sess_id, from_ts, to_ts))
            return {"taskId": tid, "status": "running",
                    "session": sess["displayName"]}
        tid = tasks.submit("export-text", _export_text_task(sess_id, fmt, from_ts, to_ts))
        return {"taskId": tid, "status": "running",
                "session": sess["displayName"], "sync": False}

    def api_export_download(q) -> tuple[bytes, str, str]:
        """任务完成后返回 (字节, content-type, 文件名)。"""
        tid = (q.get("task") or "").strip()
        t = tasks.fetch(tid)
        if not t:
            raise KeyError(tid)
        if t["status"] == "error":
            raise RuntimeError(t["error"] or "导出失败")
        if t["status"] != "done":
            raise ValueError("任务还没完成")
        res = t["result"] or {}
        if t["kind"] == "export-html":
            return (bytes.fromhex(res["zip"]), "application/zip",
                    f"{res['session']}.zip")
        return (bytes.fromhex(res["data"]), "application/octet-stream",
                f"{res['session']}.{res['ext']}")

    # ---------------------------------------------------------------- 联系人
    def api_contacts(q) -> dict:
        with _ds_lock:
            ds = get_ds()
            cons = list(ds.contacts().values())
            counts = {s["sessionId"]: s["messageCount"] for s in ds.sessions()}
            t = q.get("type")
            if t:
                cons = [c for c in cons if ds.contact_type(c["wxid"]) == t]
            if not (q.get("all") == "1"):
                cons = [c for c in cons
                        if (c.get("remark") or c.get("nickname") or "").strip()]
            kw = (q.get("q") or "").strip().lower()
            if kw:
                cons = [c for c in cons
                        if kw in (c.get("remark") or "").lower()
                        or kw in (c.get("nickname") or "").lower()
                        or kw in c["wxid"].lower()]
            if (q.get("sort") or "messages") == "name":
                cons.sort(key=lambda c: (c.get("remark") or c.get("nickname")
                                         or c["wxid"]).lower())
            else:
                cons.sort(key=lambda c: (-counts.get(c["wxid"], 0),
                                         (c.get("remark") or c.get("nickname")
                                          or c["wxid"]).lower()))
            total = len(cons)
            offset = max(0, int(q.get("offset", 0)))
            limit = max(1, min(int(q.get("limit", 100)), 500))
            page = cons[offset:offset + limit]
            return {
                "total": total, "hasMore": total > offset + len(page),
                "contacts": [{
                    "wxid": c["wxid"],
                    "displayName": ds.display_name(c["wxid"]),
                    "type": ds.contact_type(c["wxid"]),
                    "remark": c.get("remark") or "",
                    "nickname": c.get("nickname") or "",
                    "messageCount": counts.get(c["wxid"], 0),
                } for c in page],
            }

    def api_contact(q) -> dict:
        wxid = (q.get("wxid") or "").strip()
        if not wxid:
            raise ValueError("缺少 wxid 参数")
        with _ds_lock:
            ds = get_ds()
            cons = ds.contacts()
            c = cons.get(wxid)
            if not c:
                # 联系人表没有（如群成员未存好友）也允许看，用 wxid 兜底
                cand = [v for k, v in cons.items()
                        if wxid.lower() in k.lower()]
                c = cand[0] if cand else {"wxid": wxid}
            real = c["wxid"]
            out = {
                "wxid": real,
                "displayName": ds.display_name(real),
                "type": ds.contact_type(real),
                "remark": c.get("remark") or "",
                "nickname": c.get("nickname") or "",
                "alias": c.get("alias") or "",
                "sessions": [],
                "groupTop": [],
            }
            for s in ds.find_sessions(real):
                entry = {
                    "sessionId": s["sessionId"],
                    "displayName": s["displayName"],
                    "type": s["type"],
                    "messageCount": s["messageCount"],
                    "lastTime": s["lastTime"],
                }
                try:
                    from core.analytics import Analytics
                    # session_stats 收字符串（内部再 find_session），不能传 dict
                    entry["stats"] = Analytics(ds).session_stats(s["sessionId"])
                except SystemExit:
                    entry["stats"] = None
                out["sessions"].append(entry)
            # 群聊：成员发言榜
            if out["type"] == "group" and out["sessions"]:
                try:
                    from core.analytics import Analytics
                    g = Analytics(ds).group_stats(out["sessions"][0]["sessionId"])
                    out["groupTop"] = g.get("topMembers", [])
                    out["groupTotal"] = g.get("totalMessages", 0)
                except SystemExit:
                    pass
            return out

    # ---------------------------------------------------------------- 画像/年报
    def api_profile(q) -> dict:
        sess = (q.get("session") or "").strip()
        with _ds_lock:
            from core.profile import ProfileDistiller
            pd = ProfileDistiller(get_ds())
            if sess:
                return pd.contact_profile(sess)
            return pd.self_profile()

    def api_report(q) -> dict:
        year = q.get("year")
        with _ds_lock:
            from core.analytics import Analytics
            an = Analytics(get_ds())
            if year:
                return an.report(year=int(year))
            return an.report(all_time=True)

    def _persona_task():
        """TaskHub worker：全库扫描生成本人语料包（1-2 分钟，必须异步）。"""
        def run(tid):
            with _ds_lock:
                from core.persona import build_persona
                r = build_persona(get_ds(), ROOT / "persona")
                return {"corpus": r["corpus"]["corpus"],
                        "sessions": r["corpus"]["sessions"],
                        "avgLen": r["profile"]["avgLen"],
                        "styleProfile": r["styleProfile"]}
        return run

    def api_persona(payload: dict) -> dict:
        tid = tasks.submit("persona", _persona_task())
        return {"taskId": tid, "status": "running"}

    # ---------------------------------------------------------------- 账号/密钥/配置
    def api_accounts() -> dict:
        from core.dataset import load_accounts, KEYVAULT
        accounts, current = load_accounts()
        return {
            "current": current,
            "configPath": str(KEYVAULT),
            "accounts": [{
                "name": name,
                "dbPath": acc.get("dbPath") or "",
                "hasKey": bool(acc.get("keyHex")),
                "keyPreview": (acc.get("keyHex") or "")[:8] + "…" if acc.get("keyHex") else "—",
                "hasImgKey": bool(acc.get("imgKeyHex")),
                "current": name == current,
            } for name, acc in accounts.items()],
        }

    def api_account_action(payload: dict) -> dict:
        import re as _re
        from core.dataset import load_accounts, save_accounts
        action = (payload.get("action") or "").strip()
        accounts, current = load_accounts()
        if action == "add":
            name = (payload.get("name") or "").strip()
            if not name:
                raise ValueError("账号名不能为空")
            acc = dict(accounts.get(name) or {})
            acc["wxid"] = name
            db_path = (payload.get("dbPath") or "").strip()
            if db_path:
                acc["dbPath"] = db_path
            key = (payload.get("key") or "").strip().lower()
            if key:
                if not _re.fullmatch(r"[0-9a-fA-F]{64}", key):
                    raise ValueError("密钥必须是 64 位十六进制字符串")
                acc["keyHex"] = key
            img_key = (payload.get("imgKey") or "").strip().lower()
            if img_key:
                acc["imgKeyHex"] = img_key
            acc["addedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
            accounts[name] = acc
            if len(accounts) == 1:
                current = name          # 第一个账号自动设为当前
            save_accounts(accounts, current=current)
            return {"saved": True, "account": name, "current": current}
        if action == "use":
            name = (payload.get("name") or "").strip()
            if name not in accounts:
                raise LookupError(f"没有账号「{name}」")
            save_accounts(accounts, current=name)
            return {"current": name}
        if action == "remove":
            name = (payload.get("name") or "").strip()
            if name not in accounts:
                raise LookupError(f"没有账号「{name}」")
            del accounts[name]
            if current == name:
                current = next(iter(accounts), "")
            save_accounts(accounts, current=current)
            return {"removed": name, "current": current}
        raise ValueError("action 只支持 add/use/remove")

    def api_key_action(payload: dict) -> dict:
        action = (payload.get("action") or "").strip()
        if action == "set":
            k = (payload.get("keyHex") or "").strip().lower()
            if not k:
                raise ValueError("密钥不能为空")
            if len(k) != 64 or any(c not in "0123456789abcdef" for c in k):
                raise ValueError("密钥必须是 64 位十六进制字符串")
            from core.dataset import load_keyvault, save_keyvault
            data = load_keyvault()
            data["keyHex"] = k
            save_keyvault(data)
            return {"saved": True, "keyPreview": k[:8] + "…"}
        if action == "test":
            with _ds_lock:
                ds = get_ds()
            from core.sqlcipher4 import verify_password
            src = ds.db_root / "session" / "session.db"
            if not src.exists():
                return {"hmacOk": False, "db": str(src),
                        "note": "session.db 不存在（目录没配好？）"}
            ok, detail = verify_password(ds.key, str(src))
            return {"hmacOk": ok, "db": str(src), "salt": detail.get("salt")}
        raise ValueError("action 只支持 set/test")

    # ------------------------------------------------ 密钥自动获取（只读内存扫描）
    def _key_verify_db() -> Path:
        """取一个用于 HMAC 校验的库：优先 session.db（小、必存在）。"""
        with _ds_lock:
            ds = get_ds()
        src = ds.db_root / "session" / "session.db"
        return src

    def api_key_selftest() -> dict:
        """环境预检：不取密钥，只报告各环节是否具备条件（供向导提前告知风险）。"""
        from core.keyfinder import self_test
        try:
            with _ds_lock:
                ds = get_ds()
            db = ds.db_root / "session" / "session.db"
        except Exception:
            db = Path("")
        out = self_test(db)
        try:
            from core.dataset import find_user_dir, load_accounts
            _acc, cur = load_accounts()
            out["account"] = cur
            out["dataDir"] = str(find_user_dir(None, cur)) if cur else ""
        except Exception:
            pass
        return out

    def api_key_find_stream(self, params: dict, wfile, finish) -> None:
        """SSE：边扫边推送进度。事件 stage=process/mask/scan/deref/verify/done/error。

        为什么用 SSE 而不是轮询：扫描要 30 秒左右、中间有 4 个阶段，
        轮询会让进度条在阶段边界"跳变"，SSE 才能给出连续、可信的反馈。
        """
        import threading as _th
        from core.keyfinder import find_db_key

        def emit(event: str, data: dict):
            try:
                wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                            .encode("utf-8"))
                wfile.flush()
            except Exception:
                raise

        try:
            db = self._key_verify_db()
        except Exception as e:
            emit("error", {"stage": "config", "error": f"{type(e).__name__}: {e}",
                           "hint": "先在数据目录设置里指定微信账号目录。"})
            return finish()

        cancel = _th.Event()
        last_stage = {"v": ""}
        last_emit = {"t": 0.0}

        def prog(stage, cur, tot, msg):
            now = time.time()
            # 同一阶段内节流（扫描阶段回调太密），跨阶段立即推
            if stage == last_stage["v"] and now - last_emit["t"] < 0.15:
                return
            last_stage["v"] = stage
            last_emit["t"] = now
            emit("progress", {"stage": stage, "current": cur, "total": tot,
                              "message": msg})

        def run():
            try:
                r = find_db_key(db, progress=prog, cancel=cancel)
                if r.ok:
                    # 校验通过才写入密钥库：等同用户已确认
                    try:
                        from core.dataset import load_keyvault, save_keyvault
                        from core.dataset import load_accounts
                        _acc, cur = load_accounts()
                        data = load_keyvault()
                        if cur and cur in (data.get("accounts") or {}):
                            data["accounts"][cur]["keyHex"] = r.key_hex
                        else:
                            data["keyHex"] = r.key_hex
                        save_keyvault(data)
                        saved = True
                    except Exception as e:                       # 保存失败也要告知拿到了
                        saved = False
                        emit("progress", {"stage": "save", "message":
                                          f"密钥已取得但写入失败：{e}"})
                    emit("done", {**r.to_dict(), "saved": saved})
                else:
                    emit("error", r.to_dict())
            except Exception as e:
                emit("error", {"stage": "unknown",
                               "error": f"{type(e).__name__}: {e}"})
            finally:
                finish()

        _th.Thread(target=run, daemon=True, name="mosu-keyfind").start()

    # ------------------------------------------------ 大模型配置
    def api_llm_get() -> dict:
        from core.llmconfig import load_llm_config
        cfg = load_llm_config()
        cfg["apiKeyPreview"] = (
            (load_llm_config(include_key=True)["apiKey"][:6] + "…")
            if cfg["hasKey"] else "")
        return cfg

    def api_llm_set(payload: dict) -> dict:
        from core.llmconfig import save_llm_config
        base_url = (payload.get("baseUrl") or "").strip()
        api_key = (payload.get("apiKey") or "").strip()
        model = (payload.get("model") or "").strip()
        temp = payload.get("temperature")
        if temp is not None:
            try:
                temp = float(temp)
            except (TypeError, ValueError):
                raise ValueError("temperature 必须是数字")
        if not any([base_url, api_key, model, temp is not None]):
            raise ValueError("没有要保存的内容")
        cfg = save_llm_config(base_url, api_key, model, temp)
        cfg["apiKeyPreview"] = (api_key[:6] + "…") if api_key else ""
        return {"saved": True, "config": cfg}

    def api_llm_test(payload: dict) -> dict:
        from core.llmconfig import test_connection
        return test_connection((payload.get("baseUrl") or "").strip(),
                               (payload.get("apiKey") or "").strip(),
                               (payload.get("model") or "").strip())

    def api_config() -> dict:
        from core.dataset import (KEYVAULT, LEGACY_VAULT, load_accounts,
                                  load_keyvault, find_user_dir, load_key_hex)
        accounts, current = load_accounts()
        vault = load_keyvault()
        # 多账号形态下密钥在各账号条目里，顶层 keyHex 是旧单账号格式——
        # 以 load_key_hex（当前账号解析链）为准
        try:
            key_hex = load_key_hex(current) or ""
        except SystemExit:
            key_hex = ""
        img_key = ""
        if current and current in accounts:
            img_key = accounts[current].get("imgKeyHex") or ""
        return {
            "mosuHome": str(KEYVAULT.parent),
            "configPath": str(KEYVAULT),
            "legacyVaultExists": LEGACY_VAULT.exists(),
            "current": current,
            "accountCount": len(accounts),
            "hasKey": bool(key_hex),
            "keyPreview": (key_hex[:8] + "…") if key_hex else "—",
            "hasImgKey": bool(img_key),
            "dbPath": str(find_user_dir(None, current)) if current else "",
            "envOverrides": {
                "MOSU_DB_PATH": bool(os.environ.get("MOSU_DB_PATH")),
                "MOSU_KEY_HEX": bool(os.environ.get("MOSU_KEY_HEX")),
                "MOSU_IMG_KEY_HEX": bool(os.environ.get("MOSU_IMG_KEY_HEX")),
            },
        }

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

        def _guard(self, fn, *args):
            """统一错误码：不存在/参数错 → 4xx，其余 → 500 JSON，绝不裸奔。
            注意 SystemExit 不是 Exception 子类，core 层（Analytics 等）
            找不到会话时抛的就是它，必须显式捕获。"""
            try:
                self._json(fn(*args))
            except (SystemExit, LookupError, KeyError) as e:
                self._json({"error": str(e) or "不存在"}, 404)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                f = GUI_DIR / "index.html"
                if not f.exists():
                    self._json({"error": "gui/index.html 缺失"}, 500)
                    return
                self._send(f.read_bytes(), "text/html; charset=utf-8")
                return
            for prefix in ("/vendor/", "/assets/"):
                if not path.startswith(prefix):
                    continue
                # 只允许这两个静态目录内的文件，防目录穿越
                rel = path[len(prefix):].strip("/")
                base = (GUI_DIR / prefix.strip("/")).resolve()
                f = (base / rel).resolve()
                if not str(f).startswith(str(base)) or not f.is_file():
                    self._json({"error": "not found"}, 404)
                    return
                ctype = {
                    ".js": "application/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".svg": "image/svg+xml",
                    ".png": "image/png",
                    ".ico": "image/x-icon",
                }.get(f.suffix.lower(), "application/octet-stream")
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
            if path == "/api/contacts":
                self._guard(api_contacts, self._params())
                return
            if path == "/api/contact":
                self._guard(api_contact, self._params())
                return
            if path == "/api/profile":
                self._guard(api_profile, self._params())
                return
            if path == "/api/report":
                self._guard(api_report, self._params())
                return
            if path == "/api/accounts":
                self._guard(api_accounts)
                return
            if path == "/api/config":
                self._guard(api_config)
                return
            if path == "/api/key/selftest":
                self._guard(api_key_selftest)
                return
            if path == "/api/key/find":
                # SSE 长连接：自己管 header，不能用 _json（那会带 Content-Length）
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")   # 别让中间层缓冲
                self.end_headers()
                self.close_connection = False

                def _finish():
                    try:
                        self.wfile.write(b"event: end\ndata: {}\n\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                    self.close_connection = True

                try:
                    api_key_find_stream(self._params(), self.wfile, _finish)
                except Exception as e:  # noqa: BLE001
                    try:
                        self.wfile.write(
                            f"event: error\ndata: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
                            .encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        pass
                    _finish()
                return
            if path == "/api/llm":
                self._guard(api_llm_get)
                return
            if path == "/api/moments":
                self._guard(api_moments, self._params())
                return
            if path == "/api/stats/global":
                self._guard(api_stats_global)
                return
            if path == "/api/stats/contacts":
                self._guard(api_stats_contacts, self._params())
                return
            if path == "/api/stats/time":
                self._guard(api_stats_time, self._params())
                return
            if path == "/api/task":
                self._guard(api_task, self._params())
                return
            if path == "/api/export/download":
                try:
                    data, ctype, fname = api_export_download(self._params())
                except KeyError:
                    self._json({"error": "任务不存在"}, 404)
                except ValueError as e:
                    self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                else:
                    # 中文文件名：RFC 5987 filename*（pct-encoded UTF-8）
                    quoted = quote(fname)
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Disposition",
                                     f"attachment; filename*=UTF-8''{quoted}")
                    self.end_headers()
                    self.wfile.write(data)
                return
            self._json({"error": f"unknown path: {path}"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8")) \
                    if length else {}
            except Exception:
                self._json({"error": "请求体不是合法 JSON"}, 400)
                return
            if path == "/api/chat":
                query = (payload.get("message") or "").strip()
                if not query:
                    self._json({"error": "message 不能为空"}, 400)
                    return
                try:
                    self._json(bot.chat(query, conv_id="gui"))
                except SystemExit as e:
                    self._json({"error": str(e)}, 500)
                except Exception as e:  # noqa: BLE001
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            if path == "/api/stats/keywords":
                self._guard(api_stats_keywords, payload)
                return
            if path == "/api/persona":
                self._guard(api_persona, payload)
                return
            if path == "/api/account":
                self._guard(api_account_action, payload)
                return
            if path == "/api/key":
                self._guard(api_key_action, payload)
                return
            if path == "/api/llm":
                self._guard(api_llm_set, payload)
                return
            if path == "/api/llm/test":
                self._guard(api_llm_test, payload)
                return
            if path == "/api/export":
                self._guard(api_export, payload)
                return
            self._json({"error": f"unknown path: {path}"}, 404)

    # 启动时清空导出产物目录（上次运行的临时 HTML/zip，无保留价值）
    try:
        if EXPORT_ROOT.exists():
            shutil.rmtree(EXPORT_ROOT)
    except Exception:
        pass

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
