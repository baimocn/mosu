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

    def __init__(self, runner, host="127.0.0.1", port=0):
        """runner: 回调 (op: str, params: dict) -> dict，负责真正干活。

        把它做成注入点，是为了让服务端不依赖 GUI 也不依赖真实数据，
        测试里可以塞假 runner。
        """
        super().__init__((host, port), _Handler)
        self.runner = runner
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
        raise proto.DispatchError(proto.ERR_UNSUPPORTED, f"不支持的操作 {op}")

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
    """

    def __init__(self, get_ds, tasks_hub=None):
        self.get_ds = get_ds
        self.tasks = tasks_hub
        self._lock = threading.RLock()
        # 当前正在跑的那一轮（供 steer 递消息）
        self._current = None          # {"agent":..., "question":..., "runId":...}
        # 排队中的活：[(question, history, submitter)]
        self._queue: list[dict] = []
        # 每轮的完成状态，供 wait 查询
        self._done: dict[str, dict] = {}
        self._history: list[dict] = []   # 最近派活记录（给 status 看）

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
            }

    def _runs(self, data: dict) -> dict:
        from core import runs as runs_mod
        limit = int(data.get("limit") or 30)
        limit = max(1, min(limit, 100))
        return {"runs": runs_mod.list_runs(limit)}

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
            steps_box: list[dict] = []
            started = time.time()
            try:
                run_obj = agent.run(question, history=history,
                                    on_step=steps_box.append)
                out = run_obj.to_dict()
                out["model"] = agent.llm.model
                out["branches"] = [b.to_dict() for b in run_obj.branches]
                out["facts"] = []
                out["citations"] = []
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
                        "status": "done",
                        "source": "dispatch",
                    })
                except Exception:
                    pass
                with self._lock:
                    self._done[run_id] = {"status": "done", "result": out,
                                          "cost": out.get("cost")}
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._done[run_id] = {"status": "error",
                                          "error": f"{type(e).__name__}: {e}"}
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
                        return st
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
                            return {"runId": last[0], **last[1]}
                        return {"status": "idle",
                                "note": "当前没有任务，也没有已完成的任务"}
                    run_id = cur["runId"]
            time.sleep(0.25)
        return {"status": "timeout",
                "note": f"等待 {timeout:.0f} 秒仍未完成",
                "runId": run_id}
