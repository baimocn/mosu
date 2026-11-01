"""派活服务端：让外部程序（CLI / 脚本 / 别的 agent）把活派给运行中的墨溯。

对标 Lancer 的 Control API（`apps/desktop/electron/control/control-server.ts`）。
抄的语义：
  - Bearer token 鉴权 + **timing-safe** 比较
  - 统一响应信封 {"ok": bool, "result"|"error"}
  - 结构化错误码
  - 请求体限流
  - 忙的时候明确区分 send（拒绝）/ queue（排队）/ steer（插队引导）

传输层差异（见 core/dispatch.py 顶部说明）：Unix socket → localhost TCP，
因为墨溯的主战场是 Windows，而 Lancer 自己就明确不支持 Windows。

线程模型
--------
socketserver.ThreadingTCPServer，每连接一线程。请求处理本身是**短**的：
真正耗时的分析跑在后台线程里，客户端用 `wait` 操作轮询结果——
不在连接上做长任务，避免一个慢请求占着线程。
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import time
import uuid
from pathlib import Path

from core import dispatch as proto
from core.dispatch_journal import EventJournal


class _Handler(socketserver.StreamRequestHandler):
    """处理一条请求（HTTP/1.1 风格的简化协议）。

    为什么用 StreamRequestHandler 而不是 BaseHTTPRequestHandler：
    后者按方法名分发到 do_POST/do_GET，而我们的协议是「POST /v1 + JSON 体」，
    虽然能对上，但 `handle()` 会被 `handle_one_request()` 覆盖，
    自定义分发逻辑得塞进 do_POST 里，反而绕。
    StreamRequestHandler 直接给 handle()，header 自己按行读——
    协议是我们自己定的，就该自己解析，不受 http.server 的假设约束。
    """

    # 由 DispatchServer 注入
    server: "DispatchServer"

    # 最多读多少行 header（防恶意客户端发无限多行）
    MAX_HEADER_LINES = 64

    def handle(self):
        try:
            headers = self._read_headers()
        except Exception:
            self._send(400, proto.err(proto.ERR_INVALID_JSON, "请求头格式不对"))
            return

        if headers is None:
            self._send(400, proto.err(proto.ERR_INVALID_JSON, "请求头不完整"))
            return

        # ---- 鉴权（先做，避免未授权的人能通过错误信息探测协议）
        auth = headers.get("authorization", "")
        got = auth[7:] if auth.lower().startswith("bearer ") else None
        if not proto.token_matches(self.server.token, got):
            self._send(401, proto.err(proto.ERR_UNAUTHORIZED,
                                      "控制 token 不正确"))
            return

        try:
            raw = self._read_body(headers)
        except proto.DispatchError as e:
            self._send(413, proto.err(e.code, e.message))
            return
        except Exception:
            self._send(400, proto.err(proto.ERR_INVALID_JSON, "请求体读取失败"))
            return

        try:
            data = proto.parse_request(raw)
        except proto.DispatchError as e:
            self._send(400, proto.err(e.code, e.message))
            return

        op = data.get("op") or ""

        # ---- 流式响应（迭代 9）
        # NDJSON 没有 Content-Length（边跑边写），
        # 所以不能用普通的 _send()——它会算 Content-Length。
        if op in proto.STREAM_OPS:
            try:
                self._stream(data)
            except proto.DispatchError as e:
                self._send(400, proto.err(e.code, e.message))
            except Exception as e:  # noqa: BLE001
                self._send(500, proto.err(proto.ERR_INTERNAL,
                                          f"{type(e).__name__}: {e}"))
            return

        try:
            result = self.server.dispatch(data)
        except proto.DispatchError as e:
            self._send(400, proto.err(e.code, e.message))
            return
        except Exception as e:  # noqa: BLE001 —— 内部错误要转成结构化错误，不能崩服务
            self._send(500, proto.err(proto.ERR_INTERNAL,
                                      f"{type(e).__name__}: {e}"))
            return
        self._send(200, proto.ok(result))

    def _stream(self, data: dict) -> None:
        """NDJSON 流式响应。

        对标 Lancer 的 control-server.ts：
        `Content-Type: application/x-ndjson`，逐行 `JSON.stringify(event) + "\n"`。

        关键点：
        - **没有 Content-Length**（长度未知，边跑边写）。
        - 写完要 flush，否则事件卡在缓冲区里，客户端看不到实时性。
        - 客户端断开（BrokenPipe）要**安静退出**，不能当错误——
          客户端 Ctrl+C 停订阅是正常用法。
        """
        self.wfile.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/x-ndjson\r\n"
            b"Cache-Control: no-store\r\n"
            b"Connection: close\r\n\r\n")
        self.wfile.flush()
        self.server.stream_to(self.wfile, data)

    def _read_headers(self):
        """读请求头。返回 {小写名: 值}，读不完整返回 None。"""
        headers: dict[str, str] = {}
        for _ in range(self.MAX_HEADER_LINES):
            line = self.rfile.readline(8192)
            if not line:
                return None
            text = line.decode("latin-1").rstrip("\r\n")
            if not text:
                break        # 空行 = header 结束
            if ":" not in text:
                continue     # 非法行，跳过
            name, _, value = text.partition(":")
            headers[name.strip().lower()] = value.strip()
        return headers

    def _read_body(self, headers: dict) -> bytes:
        """按 Content-Length 读请求体。"""
        length = 0
        try:
            length = int(headers.get("content-length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length > proto.MAX_BODY:
            raise proto.DispatchError(proto.ERR_TOO_LARGE,
                                      f"请求体超过 {proto.MAX_BODY} 字节")
        if length <= 0:
            return b""
        buf = b""
        while len(buf) < length:
            chunk = self.rfile.read(length - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.wfile.write(
                f"HTTP/1.1 {code} OK\r\n"
                f"Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Cache-Control: no-store\r\n"
                f"Connection: close\r\n\r\n".encode("ascii") + body)
            self.wfile.flush()
        except Exception:
            pass


class DispatchServer(socketserver.ThreadingTCPServer):
    """派活服务。

    allow_reuse_address：重启时端口还在 TIME_WAIT，不开这个会绑不上。
    daemon_threads：连接线程不阻止进程退出。
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, runner, host="127.0.0.1", port=0, streamer=None):
        """runner: 回调 (op: str, params: dict) -> dict，负责真正干活。

        把它做成注入点，是为了让服务端不依赖 GUI 也不依赖真实数据，
        测试里可以塞假 runner。

        streamer: 流式回调 (wfile, params: dict) -> None。
        为 None 时 stream 操作回 UNSUPPORTED（不是崩溃）。
        """
        super().__init__((host, port), _Handler)
        self.runner = runner
        self.streamer = streamer
        self.token = proto.ensure_token()
        self.host, self.port = self.server_address[0], self.server_address[1]
        self._thread: threading.Thread | None = None

    def dispatch(self, data: dict) -> dict:
        op = data["op"]
        if op == "status":
            return self.runner("status", data)
        if op == "runs":
            return self.runner("runs", data)
        if op in ("send", "queue", "steer"):
            return self.runner(op, data)
        if op == "wait":
            return self.runner("wait", data)
        if op == "tail":
            return self.runner("tail", data)
        if op == "stop":
            return self.runner("stop", data)
        if op in proto.STREAM_OPS:
            # 走到这里说明没装 streamer —— 但 _Handler 里已经拦住了，
            # 这是兜底（防止将来加了新流式 op 却忘了实现）。
            raise proto.DispatchError(proto.ERR_UNSUPPORTED,
                                      f"操作 {op} 需要流式支持，未启用")
        raise proto.DispatchError(proto.ERR_UNSUPPORTED, f"不支持的操作 {op}")

    def stream_to(self, wfile, data: dict) -> None:
        """把流式事件写进 wfile。由注入的 streamer 实现。"""
        if self.streamer is None:
            raise proto.DispatchError(proto.ERR_UNSUPPORTED,
                                      "流式操作未启用")
        self.streamer(wfile, data)

    def serve_forever_in_thread(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True,
                                        name="mosu-dispatch")
        self._thread.start()

    def stop(self) -> None:
        try:
            self.shutdown()
        except Exception:
            pass
        try:
            self.server_close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 真实执行器：接进 Agent 引擎
# ---------------------------------------------------------------------------
class InsightRunner:
    """把派活请求接到 InsightAgent 上。

    三种模式的差别（这是 Lancer 语义的核心，不是转发）：

      send   目标正忙 → BUSY。调用方自己决定等还是换个方式。
      queue  目标正忙 → 挂进队列，当前那轮跑完自动接着跑。
      steer  目标正忙 → 把消息递给**正在跑的那一轮**，让它自己调整方向。

    对标 Lancer 的 control-service.ts:245（SESSION_BUSY）与
    composer.ts（queue/steer 两种投递方式）。

    迭代 9 新增：事件日志（journal）+ stream/tail/stop 三个操作。
    """

    def __init__(self, get_ds, tasks_hub=None, journal=None):
        self.get_ds = get_ds
        self.tasks = tasks_hub
        self._lock = threading.RLock()
        # 当前正在跑的那一轮（供 steer 递消息、stop 取消）
        self._current = None          # {"agent":..., "question":..., "runId":...}
        # 排队中的活：[(question, history, submitter)]
        self._queue: list[dict] = []
        # 每轮的完成状态，供 wait 查询
        self._done: dict[str, dict] = {}
        self._history: list[dict] = []   # 最近派活记录（给 status 看）
        # 运行时事件日志（迭代 9）。stream 操作从这里读。
        self.journal = journal if journal is not None else EventJournal()
        # 每轮的消息轨迹（tail 操作要末 N 条 + lastAssistant）
        self._messages: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------ 事件记录
    # 这三个方法是给**外部**（GUI 的洞察链路）用的：
    # GUI 的 /api/insight/ask 走自己的 TaskHub，不经过 _start，
    # 但它同样该被外部订阅端看到 → 所以事件记录的口径必须只有一个，
    # 不能各写各的。
    def note_run_started(self, run_id: str, question: str) -> None:
        """记一轮的开始。"""
        self.journal.append("runStarted", question, run_id=run_id)
        self.journal.append("status", "running", run_id=run_id)

    def note_step(self, run_id: str, step: dict) -> None:
        """记一步工具调用。

        事件日志里**只放摘要**，不放工具的完整返回——
        完整返回可能几十 KB，NDJSON 逐行推会淹掉客户端。
        要完整数据走 `runs`/`wait`。
        """
        tool = str(step.get("tool") or "")
        summary = str(step.get("summary") or "")
        kind = "steer" if tool == "steer" else (
            "toolFinished" if tool != "cancel" else "runCancelled")
        self.journal.append(kind, summary, run_id=run_id,
                             tool=tool,
                             label=str(step.get("label") or "")[:120])
        with self._lock:
            self._messages.setdefault(run_id, []).append({
                "kind": "tool",
                "tool": tool,
                "text": summary[:400],
            })

    def note_run_finished(self, run_id: str, status: str, answer: str = "",
                          error: str = "", cancelled: bool = False) -> None:
        """记一轮的结束（完成 / 失败 / 被取消）。"""
        if answer:
            with self._lock:
                self._messages.setdefault(run_id, []).append({
                    "kind": "message",
                    "role": "assistant",
                    "text": answer,
                })
        if status == "cancelled":
            self.journal.append("runCancelled", error or "外部取消",
                                run_id=run_id)
            self.journal.append("status", "cancelled", run_id=run_id)
        elif status == "error":
            self.journal.append("runFailed", error or "未知错误",
                                run_id=run_id)
            self.journal.append("status", "failed", run_id=run_id)
        else:
            self.journal.append("runCompleted", "完成", run_id=run_id)
            self.journal.append("status", "done", run_id=run_id)

    def alias_run(self, old_id: str, new_id: str) -> None:
        """把某一轮的消息轨迹改挂到另一个 id 下。

        落盘时 `save_run` 遇到同名会加后缀（不覆盖旧数据），
        返回的 id 可能 ≠ 传入的 id。GUI 把**返回值**给用户，
        所以轨迹也要跟着走，否则 `tail --run-id <用户看到的 id>` 查不到。
        """
        if not old_id or not new_id or old_id == new_id:
            return
        with self._lock:
            if old_id in self._messages:
                self._messages[new_id] = self._messages.pop(old_id)
        # 事件日志里的 runId 也要改（否则 stream --run-id 新 id 查不到）
        self.journal.alias_run(old_id, new_id)

    def mark_done(self, run_id: str, status: str, result: dict | None = None,
                  cost: float | None = None, cancelled: bool = False) -> None:
        """登记某轮的最终状态（供 wait/tail 查）。

        GUI 侧的洞察不走 `_start`，但它也是一轮真实的运行——
        `wait --run-id` / `tail --run-id` 查不到它的话，
        外部程序就只能在 GUI 和派活通道之间二选一。
        """
        with self._lock:
            self._done[run_id] = {
                "status": status,
                "result": result or {},
                "cost": cost,
                "cancelled": cancelled,
            }

    def rename_done(self, old_id: str, new_id: str) -> None:
        """把某轮的完成状态改挂到另一个 id 下（配合 alias_run）。"""
        if not old_id or not new_id or old_id == new_id:
            return
        with self._lock:
            if old_id in self._done:
                self._done[new_id] = self._done.pop(old_id)

    # ------------------------------------------------------------------ 入口
    def __call__(self, op: str, data: dict) -> dict:
        if op == "status":
            return self._status(data)
        if op == "runs":
            return self._runs(data)
        if op == "wait":
            return self._wait(data)
        if op == "send":
            return self._send(data, mode="send")
        if op == "queue":
            return self._send(data, mode="queue")
        if op == "steer":
            return self._steer(data)
        if op == "tail":
            return self._tail(data)
        if op == "stop":
            return self._stop(data)
        raise proto.DispatchError(proto.ERR_UNSUPPORTED, f"不支持的操作 {op}")

    # ------------------------------------------------------------------ 状态
    def _status(self, data: dict) -> dict:
        with self._lock:
            cur = None
            if self._current:
                cur = {"question": self._current["question"],
                       "runId": self._current["runId"],
                       "since": self._current["since"]}
            return {
                "busy": bool(self._current),
                "current": cur,
                "queued": len(self._queue),
                "queue": [{"question": q["question"], "runId": q["runId"]}
                          for q in self._queue],
                "recent": list(self._history[-10:]),
                "finished": len(self._done),
                "journal": self.journal.state(),
            }

    def _runs(self, data: dict) -> dict:
        from core import runs as runs_mod
        limit = int(data.get("limit") or 30)
        limit = max(1, min(limit, 100))
        return {"runs": runs_mod.list_runs(limit)}

    # ------------------------------------------------------------------ 流式
    def stream_events(self, wfile, data: dict) -> None:
        """把事件日志推成 NDJSON。

        对标 Lancer 的 control-service.ts 的 `operation === "stream"`：
        先订阅，再回放，最后接实时。

        **顺序很要紧**：Lancer 的 ControlStream 注释写得很明确——
        "A subscription is installed before replay so a cursor never races live
        publication."
        反过来的话，回放结束到实时开始之间产生的事件会**永久丢失**。

        终止条件：
        - 客户端断开（BrokenPipe）→ 安静退出
        - 达到 maxEvents 或总超时 → 正常收尾
        - **空闲超时**：一段时间没有任何新事件、且当前没有任务在跑
          → 正常收尾（否则会一直挂着占一条连接）

        为什么要有「空闲超时」而不是「一空闲就收尾」
        ------------------------------------------
        一空闲就收尾会**直接废掉「先订阅、后派活」这个用法**：
        外部程序最自然的用法是先开一条订阅、等着看这轮的分析过程，
        可它一连上就发现「当前没任务在跑」→ 立刻断开 → 派活时已经没人接了。
        所以空闲要**连续一段时间**才算真空闲（`idleTimeout`），
        给「订阅端先就位、派活端后发起」留出窗口。

        Lancer 那边没有这个问题：它的 `ControlStream` 是常驻的
        （`control-stream.ts` 里只有 close，没有 idle 判定），
        一直等到客户端主动断开为止。墨溯多一个 idleTimeout
        是因为派活通道是长驻服务，不能让每条订阅连接无限期挂着。
        """
        since = proto.require_cursor(data)
        run_id = (data.get("runId") or "").strip()
        max_events = proto.require_int(data, "maxEvents", 1, 5000, 500)
        timeout = float(data.get("timeout") or 300)
        timeout = max(1.0, min(timeout, 1800.0))
        # 空闲多久才算「彻底没事干」。0 = 永不因空闲而收尾（只受 timeout 约束）。
        idle_timeout = float(data.get("idleTimeout") or 0)
        idle_timeout = max(0.0, min(idle_timeout, 1800.0))
        if idle_timeout == 0 and not run_id:
            # 没指定轮次 = 全局跟随流。给一个默认空闲窗口，
            # 让「开了个订阅然后一直没人派活」不会挂满 timeout。
            idle_timeout = 30.0

        pending: list[dict] = []
        wake = threading.Event()

        def _on_event(ev: dict) -> None:
            # 订阅只收**目标轮次**的事件（runId 为空则全收）
            if run_id and ev.get("runId") != run_id:
                return
            pending.append(ev)
            wake.set()

        # ① 先订阅
        unsub = self.journal.subscribe(_on_event)
        sent = 0
        last_event_at = time.time()
        deadline = time.time() + timeout
        try:
            # ② 再回放（seq > since）
            for ev in self.journal.logs(since=since, run_id=run_id):
                if not self._write_event(wfile, ev):
                    return
                sent += 1
                last_event_at = time.time()
                if sent >= max_events:
                    return
            # ③ 接实时
            while sent < max_events and time.time() < deadline:
                if pending:
                    batch, pending[:] = list(pending), []
                    for ev in batch:
                        if not self._write_event(wfile, ev):
                            return
                        sent += 1
                        last_event_at = time.time()
                        if sent >= max_events:
                            return
                    continue
                # 没新事件 → 看是不是彻底没事干了
                with self._lock:
                    idle = (not self._current) and (not self._queue)
                    finished = run_id and run_id in self._done
                if finished:
                    break
                if idle and idle_timeout > 0 and \
                        (time.time() - last_event_at) >= idle_timeout:
                    break
                wake.wait(timeout=0.5)
                wake.clear()
        except (BrokenPipeError, ConnectionResetError):
            # 客户端主动断开：正常用法，安静退出
            return
        finally:
            try:
                unsub()
            except Exception:
                pass

    @staticmethod
    def _write_event(wfile, ev: dict) -> bool:
        """写一行 NDJSON。返回 False = 客户端已断开。"""
        try:
            wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
            wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    # ------------------------------------------------------------------ 派活
    def _send(self, data: dict, mode: str) -> dict:
        question = proto.require_str(data, "question")
        history = data.get("history") or []
        if not isinstance(history, list):
            raise proto.DispatchError(proto.ERR_INVALID_REQUEST,
                                      "history 必须是数组")
        with self._lock:
            busy = bool(self._current)
            if busy and mode == "send":
                raise proto.DispatchError(
                    proto.ERR_BUSY,
                    "目标正忙：用 mode=queue 排队，或 mode=steer 引导当前这轮")
            if busy and mode == "queue":
                qid = self._enqueue(question, history)
                return {"accepted": True, "mode": mode, "queued": True,
                        "queueId": qid,
                        "position": len(self._queue),
                        "runId": self._queue[-1]["runId"]}
            # 不忙 → 直接开跑
            run_id = self._start(question, history)
            return {"accepted": True, "mode": mode, "queued": False,
                    "runId": run_id}

    def _enqueue(self, question: str, history: list) -> str:
        qid = uuid.uuid4().hex[:12]
        self._queue.append({
            "id": qid,
            "question": question,
            "history": history,
            "runId": None,
            "createdAt": time.time(),
        })
        return qid

    def _steer(self, data: dict) -> dict:
        message = proto.require_str(data, "message", maxlen=4000)
        with self._lock:
            if not self._current:
                # 没有在跑 → 当成新任务发出去（比报错有用）
                question = data.get("question")
                if isinstance(question, str) and question.strip():
                    run_id = self._start(question.strip(),
                                         data.get("history") or [])
                    return {"accepted": True, "mode": "steer",
                            "delivered": False,
                            "note": "当前没有在跑的任务，已作为新任务发出",
                            "runId": run_id}
                raise proto.DispatchError(proto.ERR_NOT_FOUND,
                                          "当前没有在跑的任务可引导")
            agent = self._current["agent"]
            agent.steer(message)
            self._history.append({
                "op": "steer", "at": time.time(),
                "runId": self._current["runId"],
                "message": message[:200],
            })
            return {"accepted": True, "mode": "steer", "delivered": True,
                    "runId": self._current["runId"]}

    def _start(self, question: str, history: list) -> str:
        """真正开跑一轮（后台线程）。

        复用 GUI 的 InsightAgent + AgentTeam，走同一套数据层与审计，
        所以派活得到的答案与 GUI 里看到的**完全一致**（同一份代码路径）。
        """
        from core.agent_engine import InsightAgent
        from core.agent_team import AgentTeam, BranchBudget
        from core import runs as runs_mod

        # run_id 用 runs.make_run_id()，跟 GUI 侧同一套命名。
        # 早先这里用 uuid4().hex[:12]，两种格式的文件混在一个目录里，
        # 而 list_runs() 是**按文件名排序**的 → 派活的记录在历史列表里
        # 位置随机，看起来像「乱序」。id 格式必须全局统一。
        run_id = runs_mod.make_run_id(question)
        ds = self.get_ds()
        team = AgentTeam(ds, budget=BranchBudget())
        agent = InsightAgent(ds, team=team)

        def work():
            started = time.time()
            self.note_run_started(run_id, question)

            def on_step(step: dict) -> None:
                self.note_step(run_id, step)

            try:
                run_obj = agent.run(question, history=history, on_step=on_step)
                out = run_obj.to_dict()
                out["model"] = agent.llm.model
                out["branches"] = [b.to_dict() for b in run_obj.branches]
                out["facts"] = []
                out["citations"] = []
                # 助手回复进消息轨迹（tail 的 lastAssistant 从这来）
                self.note_run_finished(
                    run_id,
                    "cancelled" if out.get("cancelled") else "done",
                    answer=out.get("answer") or "",
                    error=out.get("cancelReason") or "",
                    cancelled=bool(out.get("cancelled")),
                )
                try:
                    runs_mod.save_run({
                        "id": run_id, "question": question,
                        "answer": out.get("answer", ""),
                        "steps": out.get("steps", []),
                        "evidence": out.get("evidence", []),
                        "branches": out.get("branches", []),
                        "tokens": out.get("tokens", 0),
                        "cost": out.get("cost", 0),
                        "model": out.get("model"),
                        "createdAt": time.time(),
                        "status": "cancelled" if out.get("cancelled") else "done",
                        "cancelled": bool(out.get("cancelled")),
                        "source": "dispatch",
                    })
                except Exception:
                    pass
                with self._lock:
                    self._done[run_id] = {
                        "status": "cancelled" if out.get("cancelled") else "done",
                        "result": out,
                        "cost": out.get("cost"),
                        "cancelled": bool(out.get("cancelled")),
                    }
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._done[run_id] = {"status": "error",
                                          "error": f"{type(e).__name__}: {e}"}
                self.note_run_finished(run_id, "error",
                                       error=f"{type(e).__name__}: {e}")
            finally:
                with self._lock:
                    self._current = None
                    self._history.append({
                        "op": "finish", "at": time.time(), "runId": run_id,
                        "cost": round(time.time() - started, 2),
                    })
                self._pump_queue()

        with self._lock:
            self._current = {"agent": agent, "question": question,
                             "runId": run_id, "since": time.time()}
        threading.Thread(target=work, daemon=True, name=f"mosu-dispatch-{run_id}").start()
        return run_id

    def _pump_queue(self) -> None:
        """当前跑完了 → 把队列里下一个捞起来跑。"""
        with self._lock:
            if not self._queue or self._current:
                return
            item = self._queue.pop(0)
            item["runId"] = self._start(item["question"], item["history"])

    # ------------------------------------------------------------------ 等结果
    def _tail(self, data: dict) -> dict:
        """取末 N 条消息 + 最后一条助手回复。

        对标 Lancer 的 `operation === "tail"`：
        返回 `{...result, messages: transcript.slice(-n), lastAssistant}`。

        与 stream 的分工：
        - stream 是**跟着看**（要过程）
        - tail 是**只要结论**（不要过程，但要最后几句话）

        n 的上限 1000（Lancer 同口径）。下限 1——
        n=0 会返回空列表，那不如不调。
        """
        n = proto.require_int(data, "n", 1, 1000, 20)
        run_id = (data.get("runId") or "").strip()
        with self._lock:
            if run_id:
                msgs = list(self._messages.get(run_id) or [])
                done = self._done.get(run_id)
            else:
                # 不传 runId → 取最近一轮
                if self._current:
                    run_id = self._current["runId"]
                elif self._done:
                    run_id = list(self._done.keys())[-1]
                else:
                    run_id = ""
                msgs = list(self._messages.get(run_id) or []) if run_id else []
                done = self._done.get(run_id) if run_id else None
        tail_msgs = msgs[-n:]
        last_assistant = None
        for m in reversed(msgs):
            if m.get("kind") == "message" and m.get("role") == "assistant":
                last_assistant = m.get("text") or ""
                break
        out = {
            "runId": run_id,
            "messages": tail_msgs,
            "lastAssistant": last_assistant,
            "n": n,
        }
        if done:
            out["status"] = done.get("status")
            out["done"] = done.get("status") in ("done", "error", "cancelled")
        else:
            out["status"] = "running" if run_id else "idle"
            out["done"] = False
        return out

    def _stop(self, data: dict) -> dict:
        """取消正在跑的那一轮。

        对标 Lancer 的 `operation === "stop"`。

        语义：
        - 目标不在跑 → NOT_CANCELLABLE（明确说「没在跑」，
          而不是假装取消成功）
        - 取消是**协作式**的：主循环在下一步检查到标志才停。
          所以这里会等一小会儿确认它真的停了。
        - 等不到（工具调用卡住）→ 仍然返回 accepted=True，
          但 cancelled=False + note 说明「已请求但还没停下」。
          撒谎说停了比说没停更糟。
        """
        run_id = (data.get("runId") or "").strip()
        reason = (data.get("reason") or "").strip()[:200]
        with self._lock:
            cur = self._current
            if not cur:
                raise proto.DispatchError(
                    proto.ERR_NOT_CANCELLABLE,
                    "当前没有在跑的任务，没什么可取消")
            if run_id and cur["runId"] != run_id:
                raise proto.DispatchError(
                    proto.ERR_NOT_FOUND,
                    f"运行中的是 {cur['runId']}，不是 {run_id}")
            agent = cur["agent"]
            target = cur["runId"]
        accepted = agent.cancel(reason or "外部请求取消")
        stopped = agent.wait_cancelled(timeout=float(data.get("timeout") or 15))
        self.journal.append("status", "cancelling" if not stopped else "cancelled",
                            run_id=target)
        return {
            "accepted": accepted,
            "stopped": bool(stopped),
            "runId": target,
            "note": ("已停止" if stopped else
                     "已请求取消，但主循环还没停下（可能卡在一次工具调用里）"),
        }

    def _wait(self, data: dict) -> dict:
        """等某轮跑完。

        支持两种用法：
          wait {"runId": "..."}   等指定的一轮
          wait {"timeout": 300}   等「当前正在跑的那轮」（不传 runId 时）
        """
        timeout = float(data.get("timeout") or 300)
        timeout = max(1.0, min(timeout, 1800.0))
        run_id = data.get("runId")
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if run_id:
                    st = self._done.get(run_id)
                    if st:
                        # 取消也要能被 wait 看到——不能装作「还在跑」
                        # （那会让调用方一直等到超时）。
                        return {"runId": run_id, **st,
                                "done": st.get("status") in
                                ("done", "error", "cancelled")}
                    # 可能还在跑
                    if self._current and self._current["runId"] == run_id:
                        pass
                    else:
                        # 跑过但记录被清？当作还在跑（继续等）
                        pass
                else:
                    cur = self._current
                    if not cur:
                        # 没有在跑的 → 看最近完成的一轮
                        if self._done:
                            last = list(self._done.items())[-1]
                            return {"runId": last[0], **last[1],
                                    "done": last[1].get("status") in
                                    ("done", "error", "cancelled")}
                        return {"status": "idle", "done": False,
                                "note": "当前没有任务，也没有已完成的任务"}
                    run_id = cur["runId"]
            time.sleep(0.25)
        return {"status": "timeout", "done": False,
                "note": f"等待 {timeout:.0f} 秒仍未完成",
                "runId": run_id}
