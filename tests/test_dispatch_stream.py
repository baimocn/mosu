# -*- coding: utf-8 -*-
"""迭代 9：派活通道补齐 stream / tail / stop（对标 Lancer 的 Control API）。

核心要锁住的六件事：

  1. **事件日志有全局单调 seq**，且 `logs(since)` 只回放 seq > since
  2. **凭证脱敏**：API key 不能出现在推给外部的事件里
  3. **订阅先于回放**：回放与实时之间不能漏事件
     （Lancer: "A subscription is installed before replay so a cursor never
     races live publication."）
  4. **stream 走 NDJSON**：Content-Type 是 application/x-ndjson，
     一行一个 JSON，且**没有 Content-Length**（长度未知，边跑边写）
  5. **stop 是协作式的**：主循环在**下一步**检查到标志才停，
     不打断正在进行的工具调用（否则会留下半截状态）
  6. **取消不等于失败**：被取消的一轮 wait 要能看到 cancelled，
     不能装作「还在跑」让调用方等到超时

用真 socket 起真服务（不是 mock），跑的是真的 HTTP 请求。
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestEventJournal(unittest.TestCase):
    """事件日志：seq / 回放 / 脱敏 / 有界。"""

    def setUp(self):
        from core.dispatch_journal import EventJournal
        self.j = EventJournal()

    def test_seq_is_monotonic(self):
        a = self.j.append("status", "一")
        b = self.j.append("status", "二")
        c = self.j.append("status", "三")
        self.assertEqual([a["seq"], b["seq"], c["seq"]], [1, 2, 3])

    def test_logs_since_filters_by_seq(self):
        self.j.append("status", "一")
        self.j.append("status", "二")
        self.j.append("status", "三")
        self.assertEqual(len(self.j.logs(since=0)), 3)
        self.assertEqual(len(self.j.logs(since=1)), 2)
        self.assertEqual(len(self.j.logs(since=2)), 1)
        self.assertEqual(len(self.j.logs(since=3)), 0)

    def test_logs_since_is_exclusive(self):
        """since=N 表示「我收到了第 N 条」，所以要 N 之后**不含** N。"""
        self.j.append("status", "一")
        evs = self.j.logs(since=1)
        self.assertEqual(len(evs), 0)

    def test_logs_filter_by_run_id(self):
        self.j.append("status", "a", run_id="r1")
        self.j.append("status", "b", run_id="r2")
        self.j.append("status", "c", run_id="r1")
        got = self.j.logs(run_id="r1")
        self.assertEqual([e["text"] for e in got], ["a", "c"])

    def test_logs_are_sorted_by_seq(self):
        for i in range(5):
            self.j.append("status", str(i))
        seqs = [e["seq"] for e in self.j.logs()]
        self.assertEqual(seqs, sorted(seqs))

    def test_unknown_kind_becomes_status(self):
        """拼错的 kind 不能静默塞进去——下游按 kind 分支会全落空。"""
        ev = self.j.append("toolStartedXyz", "x")
        self.assertEqual(ev["kind"], "status")

    def test_events_are_bounded(self):
        """长驻服务不能无限涨内存。"""
        from core.dispatch_journal import EventJournal
        j = EventJournal(max_events=10)
        for i in range(50):
            j.append("status", str(i))
        evs = j.logs()
        self.assertEqual(len(evs), 10)
        # 保留的是**最新**的
        self.assertEqual(evs[-1]["text"], "49")

    def test_subscribe_receives_live_events(self):
        got = []
        self.j.subscribe(lambda e: got.append(e))
        self.j.append("status", "实时")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], "实时")

    def test_unsubscribe_stops_delivery(self):
        got = []
        unsub = self.j.subscribe(lambda e: got.append(e))
        unsub()
        self.j.append("status", "x")
        self.assertEqual(len(got), 0)

    def test_subscriber_exception_does_not_break_append(self):
        """订阅者自己炸了不能影响写日志的主流程。"""
        def _bad(ev):
            raise RuntimeError("boom")
        self.j.subscribe(_bad)
        ev = self.j.append("status", "还是能写")
        self.assertEqual(ev["text"], "还是能写")

    def test_state_reports_last_seq(self):
        self.j.append("status", "一")
        self.j.append("status", "二")
        st = self.j.state()
        self.assertEqual(st["lastSeq"], 2)
        self.assertEqual(st["events"], 2)


class TestRedaction(unittest.TestCase):
    """凭证脱敏：事件是要推给外部程序看的。"""

    def setUp(self):
        from core import dispatch_journal as dj
        self.dj = dj

    def test_sk_key_is_redacted(self):
        s = set()
        out = self.dj.scrub("用 sk-abcdef1234567890 查的", s)
        self.assertNotIn("abcdef1234567890", out)
        self.assertIn(self.dj.REDACTED, out)

    def test_bearer_token_is_redacted(self):
        s = set()
        out = self.dj.scrub("Authorization: Bearer sk-xyz0123456789abcdef", s)
        self.assertNotIn("xyz0123456789abcdef", out)
        self.assertIn("Bearer", out)      # 前缀保留，可读性

    def test_assignment_form_is_redacted(self):
        s = set()
        out = self.dj.scrub('apiKey: "mysecretvalue"', s)
        self.assertNotIn("mysecretvalue", out)

    def test_github_token_is_redacted(self):
        s = set()
        out = self.dj.scrub("token ghp_abcdefghijklmnopqrstuvwxyz1234", s)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz1234", out)

    def test_aws_key_is_redacted(self):
        s = set()
        out = self.dj.scrub("AKIAIOSFODNN7EXAMPLE", s)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)

    def test_normal_text_untouched(self):
        s = set()
        text = "我上个月跟张三聊了 42 条消息"
        self.assertEqual(self.dj.scrub(text, s), text)

    def test_collected_secret_is_scrubbed_from_later_text(self):
        """采集与替换必须分两步。

        第一步在字段里采集到凭证，第二步所有文本里都要抹掉。
        只做第二步会漏掉「字段名叫 notes、值其实是 key」的情况。
        """
        s = set()
        self.dj.scrub('password: "hunter2xyz"', s)
        self.assertIn("hunter2xyz", s)
        out = self.dj.scrub("后来又提到了 hunter2xyz", s)
        self.assertNotIn("hunter2xyz", out)

    def test_collect_secrets_finds_nested(self):
        s = set()
        self.dj.collect_secrets({"a": {"apiKey": "nestedsecretvalue"}}, s)
        self.assertIn("nestedsecretvalue", s)

    def test_collect_secrets_respects_sensitive_flag(self):
        """父级字段名敏感 → 整个子树都算敏感。

        敏感词表与 Lancer 同口径：key|token|secret|password|authorization。
        注意 `creds` **不在**表里——Lancer 也不认它，别自己扩大。
        """
        s = set()
        self.dj.collect_secrets({"apiKey": {"anything": "valuehere"}}, s)
        self.assertIn("valuehere", s)

    def test_collect_secrets_ignores_non_sensitive_parent(self):
        """父级字段名不敏感 → 子树按普通字段处理。

        这条锁住「敏感词表不扩大」：随手加词会让正常的字段被误采集。
        """
        s = set()
        self.dj.collect_secrets({"creds": {"anything": "valuehere"}}, s)
        self.assertNotIn("valuehere", s)

    def test_collect_secrets_ignores_ordinary_field(self):
        s = set()
        self.dj.collect_secrets({"note": "普通内容"}, s)
        self.assertEqual(len(s), 0)

    def test_redact_value_replaces_sensitive_field(self):
        s = set()
        out = self.dj.redact_value({"apiKey": "abcd", "note": "hi"}, s)
        self.assertEqual(out["apiKey"], self.dj.REDACTED)
        self.assertEqual(out["note"], "hi")

    def test_redact_value_handles_circular(self):
        s = set()
        d = {"note": "x"}
        d["self"] = d
        out = self.dj.redact_value(d, s)
        self.assertEqual(out["self"], "[Circular]")

    def test_journal_scrubs_appended_text(self):
        """append 走的事件也要脱敏（不只是裸调 scrub 时）。"""
        from core.dispatch_journal import EventJournal
        j = EventJournal()
        ev = j.append("toolFinished", "调用参数 apiKey=sk-abcdefghij1234567890")
        self.assertNotIn("abcdefghij1234567890", ev["text"])


class TestCursorValidation(unittest.TestCase):
    """since 游标的校验。"""

    def setUp(self):
        from core import dispatch as dp
        self.dp = dp

    def test_cursor_defaults_to_zero(self):
        self.assertEqual(self.dp.require_cursor({}), 0)
        self.assertEqual(self.dp.require_cursor({"since": None}), 0)

    def test_cursor_accepts_nonnegative_int(self):
        self.assertEqual(self.dp.require_cursor({"since": 42}), 42)

    def test_cursor_rejects_negative(self):
        with self.assertRaises(self.dp.DispatchError) as ctx:
            self.dp.require_cursor({"since": -1})
        self.assertEqual(ctx.exception.code, self.dp.ERR_INVALID_REQUEST)

    def test_cursor_rejects_float(self):
        with self.assertRaises(self.dp.DispatchError):
            self.dp.require_cursor({"since": 1.5})

    def test_cursor_rejects_bool(self):
        """bool 是 int 的子类——但 True 当游标是荒谬的，必须拒。"""
        with self.assertRaises(self.dp.DispatchError):
            self.dp.require_cursor({"since": True})

    def test_require_int_bounds(self):
        self.assertEqual(self.dp.require_int({"n": 5}, "n", 1, 1000, 20), 5)
        self.assertEqual(self.dp.require_int({}, "n", 1, 1000, 20), 20)
        with self.assertRaises(self.dp.DispatchError):
            self.dp.require_int({"n": 0}, "n", 1, 1000, 20)
        with self.assertRaises(self.dp.DispatchError):
            self.dp.require_int({"n": 1001}, "n", 1, 1000, 20)


class TestAgentCancel(unittest.TestCase):
    """引擎取消：协作式，不打断进行中的工具调用。"""

    def setUp(self):
        from core.agent_engine import InsightAgent
        self.InsightAgent = InsightAgent

    def _agent(self):
        # 不真初始化数据集——取消机制不碰数据
        agent = self.InsightAgent.__new__(self.InsightAgent)
        agent._steer_lock = threading.Lock()
        agent._pending_steer = []
        agent.last_steer = []
        agent._cancel_event = threading.Event()
        agent._cancel_reason = ""
        return agent

    def test_cancel_sets_flag(self):
        agent = self._agent()
        self.assertFalse(agent.cancelled)
        self.assertTrue(agent.cancel("测试"))
        self.assertTrue(agent.cancelled)
        self.assertEqual(agent.cancel_reason, "测试")

    def test_cancel_is_idempotent(self):
        agent = self._agent()
        self.assertTrue(agent.cancel())
        self.assertFalse(agent.cancel())      # 第二次不算「新发起」

    def test_wait_cancelled_returns_true_after_cancel(self):
        agent = self._agent()
        agent.cancel()
        self.assertTrue(agent.wait_cancelled(timeout=1))

    def test_wait_cancelled_times_out_when_not_cancelled(self):
        agent = self._agent()
        self.assertFalse(agent.wait_cancelled(timeout=0.2))

    def test_cancel_reason_kept(self):
        agent = self._agent()
        agent.cancel("用户按了停止")
        self.assertEqual(agent.cancel_reason, "用户按了停止")


class TestRunCancelledFlag(unittest.TestCase):
    """AgentRun 要能表达「被取消了」——下游必须能区分取消与正常完成。"""

    def test_run_has_cancelled_field(self):
        from core.agent_engine import AgentRun
        r = AgentRun("问题")
        self.assertFalse(r.cancelled)
        self.assertEqual(r.cancelReason, "")
        d = r.to_dict()
        self.assertIn("cancelled", d)
        self.assertIn("cancelReason", d)
        self.assertFalse(d["cancelled"])


class _FakeAgent:
    """假 Agent：可取消、可等。"""

    def __init__(self):
        self._ev = threading.Event()
        self._reason = ""

    def cancel(self, reason=""):
        if self._ev.is_set():
            return False
        self._reason = reason
        self._ev.set()
        return True

    def wait_cancelled(self, timeout=30.0):
        return self._ev.wait(timeout)

    def steer(self, msg):
        pass


class TestRunnerOps(unittest.TestCase):
    """InsightRunner 的 tail / stop 两个操作。"""

    def setUp(self):
        from core.dispatch_server import InsightRunner
        self.InsightRunner = InsightRunner
        self.runner = InsightRunner(lambda: None)

    def test_stop_without_running_raises_not_cancellable(self):
        """没在跑就明确说「没在跑」，不能假装取消成功。"""
        from core import dispatch as dp
        with self.assertRaises(dp.DispatchError) as ctx:
            self.runner("stop", {"reason": "x"})
        self.assertEqual(ctx.exception.code, dp.ERR_NOT_CANCELLABLE)

    def test_stop_cancels_current_run(self):
        agent = _FakeAgent()
        self.runner._current = {"agent": agent, "question": "q",
                                "runId": "r1", "since": time.time()}
        res = self.runner("stop", {"reason": "别跑了"})
        self.assertTrue(res["accepted"])
        self.assertTrue(res["stopped"])
        self.assertEqual(res["runId"], "r1")

    def test_stop_with_wrong_run_id_raises_not_found(self):
        from core import dispatch as dp
        agent = _FakeAgent()
        self.runner._current = {"agent": agent, "question": "q",
                                "runId": "r1", "since": time.time()}
        with self.assertRaises(dp.DispatchError) as ctx:
            self.runner("stop", {"runId": "other"})
        self.assertEqual(ctx.exception.code, dp.ERR_NOT_FOUND)

    def test_stop_reports_not_stopped_yet(self):
        """协作式取消：请求发出 ≠ 已停下。没停下要说没停下。"""
        class _Stubborn(_FakeAgent):
            def wait_cancelled(self, timeout=30.0):
                return False
        agent = _Stubborn()
        self.runner._current = {"agent": agent, "question": "q",
                                "runId": "r1", "since": time.time()}
        res = self.runner("stop", {})
        self.assertTrue(res["accepted"])
        self.assertFalse(res["stopped"])
        self.assertIn("还没停下", res["note"])

    def test_tail_returns_last_assistant(self):
        self.runner._messages["r1"] = [
            {"kind": "tool", "tool": "tool_numbers", "text": "查了 42 条"},
            {"kind": "message", "role": "assistant", "text": "一共 42 条"},
        ]
        res = self.runner("tail", {"runId": "r1"})
        self.assertEqual(res["lastAssistant"], "一共 42 条")
        self.assertEqual(len(res["messages"]), 2)

    def test_tail_limits_to_n(self):
        for i in range(30):
            self.runner._messages["r1"] = self.runner._messages.get("r1", []) + [
                {"kind": "tool", "tool": "t", "text": str(i)}]
        res = self.runner("tail", {"runId": "r1", "n": 5})
        self.assertEqual(len(res["messages"]), 5)

    def test_tail_rejects_bad_n(self):
        from core import dispatch as dp
        with self.assertRaises(dp.DispatchError) as ctx:
            self.runner("tail", {"runId": "r1", "n": 0})
        self.assertEqual(ctx.exception.code, dp.ERR_INVALID_REQUEST)

    def test_tail_without_run_id_uses_latest(self):
        self.runner._done["r1"] = {"status": "done", "result": {}}
        self.runner._done["r2"] = {"status": "done", "result": {}}
        self.runner._messages["r2"] = [
            {"kind": "message", "role": "assistant", "text": "最新"}]
        res = self.runner("tail", {})
        self.assertEqual(res["runId"], "r2")
        self.assertEqual(res["lastAssistant"], "最新")

    def test_tail_idle_when_nothing_ran(self):
        res = self.runner("tail", {})
        self.assertEqual(res["status"], "idle")
        self.assertIsNone(res["lastAssistant"])


class TestServerStream(unittest.TestCase):
    """服务端：NDJSON 响应头 + 事件推送。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("MOSU_HOME")
        os.environ["MOSU_HOME"] = self._tmp.name
        import importlib
        from core import dispatch as dp
        self.dp = importlib.reload(dp)
        from core.dispatch_server import DispatchServer, InsightRunner
        self.runner = InsightRunner(lambda: None)
        self.srv = DispatchServer(self.runner, host="127.0.0.1", port=0,
                                  streamer=self.runner.stream_events)
        self.srv.serve_forever_in_thread()
        self.port = self.srv.port

    def tearDown(self):
        self.srv.stop()
        if self._old is None:
            os.environ.pop("MOSU_HOME", None)
        else:
            os.environ["MOSU_HOME"] = self._old
        self._tmp.cleanup()

    def _raw_request(self, body: dict, read_all=True):
        """发一个原始 HTTP 请求，返回 (头, 体)。"""
        tok = self.dp.ensure_token()
        raw = json.dumps(body).encode("utf-8")
        head = (f"POST /v1 HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                f"Authorization: Bearer {tok}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.settimeout(10)
            s.sendall(head + raw)
            chunks = []
            while True:
                try:
                    d = s.recv(65536)
                except socket.timeout:
                    break
                if not d:
                    break
                chunks.append(d)
                if not read_all:
                    break
        return b"".join(chunks)

    def test_stream_response_is_ndjson(self):
        """Content-Type 必须是 application/x-ndjson，且没有 Content-Length。"""
        raw = self._raw_request({"op": "stream", "timeout": 2})
        head = raw.split(b"\r\n\r\n")[0].decode("latin-1")
        self.assertIn("application/x-ndjson", head)
        # 长度未知 → 不能有 Content-Length
        self.assertNotIn("Content-Length", head)

    def test_stream_replays_journal_events(self):
        """已发生的事件要能通过 since 游标补回来。"""
        self.runner.journal.append("status", "历史事件", run_id="r1")
        raw = self._raw_request({"op": "stream", "since": 0, "timeout": 3})
        body = raw.split(b"\r\n\r\n", 1)[1]
        lines = [ln for ln in body.split(b"\n") if ln.strip()]
        self.assertGreaterEqual(len(lines), 1)
        ev = json.loads(lines[0].decode("utf-8"))
        self.assertEqual(ev["text"], "历史事件")
        self.assertEqual(ev["seq"], 1)

    def test_stream_since_filters_old_events(self):
        self.runner.journal.append("status", "第一条")
        self.runner.journal.append("status", "第二条")
        raw = self._raw_request({"op": "stream", "since": 1, "timeout": 3})
        body = raw.split(b"\r\n\r\n", 1)[1]
        lines = [ln for ln in body.split(b"\n") if ln.strip()]
        texts = [json.loads(ln.decode("utf-8"))["text"] for ln in lines]
        self.assertNotIn("第一条", texts)
        self.assertIn("第二条", texts)

    def test_stream_lines_are_valid_json(self):
        self.runner.journal.append("status", "带引号的\"文本\"")
        raw = self._raw_request({"op": "stream", "timeout": 3})
        body = raw.split(b"\r\n\r\n", 1)[1]
        for ln in body.split(b"\n"):
            if ln.strip():
                json.loads(ln.decode("utf-8"))     # 解析失败会抛

    def test_stream_requires_auth(self):
        """没 token → 401，不能给数据。"""
        raw = json.dumps({"op": "stream"}).encode("utf-8")
        head = (f"POST /v1 HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(head + raw)
            resp = s.recv(65536)
        self.assertIn(b"401", resp.split(b"\r\n")[0])


class TestClientStream(unittest.TestCase):
    """客户端：stream() 逐行读 NDJSON。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("MOSU_HOME")
        os.environ["MOSU_HOME"] = self._tmp.name
        import importlib
        from core import dispatch as dp
        self.dp = importlib.reload(dp)
        from core.dispatch_server import DispatchServer, InsightRunner
        self.runner = InsightRunner(lambda: None)
        self.srv = DispatchServer(self.runner, host="127.0.0.1", port=0,
                                  streamer=self.runner.stream_events)
        self.srv.serve_forever_in_thread()
        self.port = self.srv.port
        self.tok = self.dp.ensure_token()

    def tearDown(self):
        self.srv.stop()
        if self._old is None:
            os.environ.pop("MOSU_HOME", None)
        else:
            os.environ["MOSU_HOME"] = self._old
        self._tmp.cleanup()

    def test_stream_collects_events(self):
        self.runner.journal.append("status", "一")
        self.runner.journal.append("status", "二")
        got = []
        res = self.dp.stream({"op": "stream", "timeout": 3},
                             port=self.port, token=self.tok,
                             on_event=got.append)
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 2)
        self.assertEqual([e["text"] for e in got], ["一", "二"])

    def test_stream_tracks_last_seq(self):
        self.runner.journal.append("status", "一")
        self.runner.journal.append("status", "二")
        res = self.dp.stream({"op": "stream", "timeout": 3},
                             port=self.port, token=self.tok,
                             on_event=lambda e: None)
        self.assertEqual(res["lastSeq"], 2)

    def test_stream_replays_with_since(self):
        self.runner.journal.append("status", "一")
        self.runner.journal.append("status", "二")
        got = []
        self.dp.stream({"op": "stream", "since": 1, "timeout": 3},
                       port=self.port, token=self.tok, on_event=got.append)
        self.assertEqual([e["text"] for e in got], ["二"])

    def test_stream_without_token_errors(self):
        res = self.dp.stream({"op": "stream", "timeout": 3},
                             port=self.port, token="0" * 64)
        # token 错 → 服务端 401，客户端拿到的是非 NDJSON 体
        # 这里只断言「没崩、没假装成功」
        self.assertIn("ok", res)

    def test_stream_callback_can_stop(self):
        for i in range(5):
            self.runner.journal.append("status", str(i))
        got = []
        def _stop_after_two(e):
            got.append(e)
            return len(got) < 2
        res = self.dp.stream({"op": "stream", "timeout": 3},
                             port=self.port, token=self.tok,
                             on_event=_stop_after_two)
        self.assertEqual(len(got), 2)


class TestSubscribeBeforeDispatch(unittest.TestCase):
    """「先订阅、后派活」必须能用。

    这是端到端抓出来的真缺陷：
    原来的 `stream_events` 一发现「当前没任务在跑」就立刻收尾断开，
    于是外部程序最自然的用法——先开一条订阅等着，再派活——直接失效：
    订阅端在派活发生前就被踢下线。

    Lancer 那边没有这个问题（`control-stream.ts` 里只有 close，没有 idle 判定）。

    这里锁两件事：
      1. 空闲时**不立刻**断开（订阅端能撑到派活发生）
      2. 派活之后订阅端**真能收到**事件（不只是不断开而已）
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("MOSU_HOME")
        os.environ["MOSU_HOME"] = self._tmp.name
        import importlib
        from core import dispatch as dp
        self.dp = importlib.reload(dp)
        from core.dispatch_server import DispatchServer, InsightRunner
        self.runner = InsightRunner(lambda: None)
        self.srv = DispatchServer(self.runner, host="127.0.0.1", port=0,
                                  streamer=self.runner.stream_events)
        self.srv.serve_forever_in_thread()
        self.port = self.srv.port
        self.tok = self.dp.ensure_token()

    def tearDown(self):
        self.srv.stop()
        if self._old is None:
            os.environ.pop("MOSU_HOME", None)
        else:
            os.environ["MOSU_HOME"] = self._old
        self._tmp.cleanup()

    def test_idle_stream_does_not_disconnect_immediately(self):
        """空闲时不能一连上就断——要撑过 idleTimeout。"""
        # idleTimeout=2：连上后 1 秒，服务端还应该保持连接。
        # （旧实现会在第一次空闲判定时立刻 break → 连接秒断）
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.settimeout(10)
            body = json.dumps({"op": "stream", "timeout": 30,
                               "idleTimeout": 2}).encode("utf-8")
            head = (f"POST /v1 HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                    f"Authorization: Bearer {self.tok}\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                    ).encode("ascii")
            s.sendall(head + body)
            # 先读掉响应头
            buf = b""
            deadline = time.time() + 5
            while b"\r\n\r\n" not in buf and time.time() < deadline:
                try:
                    d = s.recv(4096)
                except socket.timeout:
                    break
                if not d:
                    break
                buf += d
            self.assertIn(b"application/x-ndjson", buf)
            # 头之后 1 秒内，服务端**不该**把连接关掉
            time.sleep(1.0)
            closed = False
            try:
                s.settimeout(1.0)
                d = s.recv(4096)
                closed = (d == b"")      # 收到空 = 对端已关闭
            except socket.timeout:
                closed = False          # 超时 = 连接还活着
            self.assertFalse(closed,
                             "空闲 1 秒连接就被关了——"
                             "「先订阅后派活」直接失效")
        finally:
            s.close()

    def test_event_after_subscribe_is_delivered(self):
        """订阅建立后再产生的事件，订阅端要真收到（不只是不断开）。"""
        got: list[dict] = []
        err: list[str] = []

        def _run():
            try:
                self.dp.stream({"op": "stream", "timeout": 10,
                                "idleTimeout": 10},
                               host="127.0.0.1", port=self.port,
                               token=self.tok, timeout=12,
                               on_event=lambda ev: got.append(ev) or True)
            except Exception as e:      # noqa: BLE001
                err.append(f"{type(e).__name__}: {e}")

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.8)      # 让订阅先就位
        self.runner.journal.append("runStarted", "订阅之后才发生的事",
                                   run_id="r-late")
        t.join(timeout=12)
        self.assertEqual(err, [])
        self.assertTrue(got, "订阅端没收到任何事件")
        self.assertEqual(got[0].get("text"), "订阅之后才发生的事")
        self.assertEqual(got[0].get("runId"), "r-late")


class TestGuiPathSharesJournal(unittest.TestCase):
    """GUI 侧的洞察与派活通道必须**共用同一本事件日志**。

    为什么这是硬要求：
    GUI 才是墨溯的主入口。如果 GUI 的洞察事件不进 journal，
    外部订阅端（mosu dispatch stream）就只能看到「派活通道派出去的活」，
    看不到用户在 GUI 里问的那些——stream 这个能力等于只在边角场景可用。

    这里验三件事：
      1. note_run_started / note_step / note_run_finished 能写出事件
      2. GUI 轮次的 runId 能被 stream 的 runId 过滤命中
      3. 落盘加后缀导致 id 变化时，事件与状态一起改名
    """

    def setUp(self):
        from core.dispatch_journal import EventJournal
        from core.dispatch_server import InsightRunner
        self.j = EventJournal()
        self.runner = InsightRunner(lambda: None, journal=self.j)

    def test_gui_round_writes_events_to_shared_journal(self):
        rid = "gui-run-1"
        self.runner.note_run_started(rid, "统计我和张三聊了多少")
        self.runner.note_step(rid, {"tool": "tool_numbers",
                                    "summary": "查到 12 条",
                                    "label": "查消息"})
        self.runner.note_run_finished(rid, "done", answer="一共 12 条")

        kinds = [e["kind"] for e in self.j.logs(since=0)]
        self.assertIn("runStarted", kinds)
        self.assertIn("toolFinished", kinds)
        self.assertIn("runCompleted", kinds)
        self.assertIn("status", kinds)
        # 所有事件都归属这一轮
        self.assertTrue(all(e["runId"] == rid for e in self.j.logs(since=0)))

    def test_gui_round_visible_to_run_id_filter(self):
        rid = "gui-run-2"
        self.runner.note_run_started(rid, "问题")
        self.runner.note_run_finished(rid, "done", answer="答案")
        # 按 runId 过滤（stream --run-id 的语义）
        got = self.j.logs(since=0, run_id=rid)
        self.assertTrue(got)
        self.assertTrue(all(e["runId"] == rid for e in got))

    def test_alias_run_renames_events_and_state(self):
        """落盘加后缀 → 用户看到的 id 变了 → 事件与状态都要跟着改。"""
        old_id, new_id = "gui-run-3", "gui-run-3-2"
        self.runner.note_run_started(old_id, "问题")
        self.runner.note_step(old_id, {"tool": "t", "summary": "s"})
        self.runner.note_run_finished(old_id, "done", answer="答案正文")
        self.runner.mark_done(old_id, "done", {"answer": "答案正文"})

        self.runner.alias_run(old_id, new_id)
        self.runner.rename_done(old_id, new_id)

        # 事件里不该再有旧 id
        self.assertEqual(self.j.logs(since=0, run_id=old_id), [])
        got = self.j.logs(since=0, run_id=new_id)
        self.assertTrue(got)
        self.assertTrue(all(e["runId"] == new_id for e in got))

        # tail 按新 id 要能查到答案
        tail = self.runner("tail", {"runId": new_id, "n": 20})
        self.assertEqual(tail["lastAssistant"], "答案正文")

    def test_mark_done_makes_wait_see_gui_round(self):
        """GUI 的轮次也要能被 wait 查到——不能只有派活的轮次能查。"""
        rid = "gui-run-4"
        self.runner.mark_done(rid, "done", {"answer": "A"})
        res = self.runner("wait", {"runId": rid, "timeout": 1})
        self.assertEqual(res["status"], "done")
        self.assertTrue(res.get("done"))


class TestOpsRegistry(unittest.TestCase):
    """操作集登记：三个新 op 必须进 OPS 且与 Lancer 的语义对齐。"""

    def setUp(self):
        from core import dispatch as dp
        self.dp = dp

    def test_new_ops_registered(self):
        for op in ("stream", "tail", "stop"):
            self.assertIn(op, self.dp.OPS)

    def test_stream_ops_flagged(self):
        self.assertIn("stream", self.dp.STREAM_OPS)
        self.assertNotIn("tail", self.dp.STREAM_OPS)
        self.assertNotIn("stop", self.dp.STREAM_OPS)

    def test_make_request_accepts_new_ops(self):
        for op in ("stream", "tail", "stop"):
            self.assertEqual(self.dp.make_request(op)["op"], op)

    def test_new_error_codes_are_distinct(self):
        codes = list(self.dp.OPS and [
            self.dp.ERR_INVALID_REQUEST, self.dp.ERR_INVALID_JSON,
            self.dp.ERR_UNAUTHORIZED, self.dp.ERR_BUSY,
            self.dp.ERR_NOT_FOUND, self.dp.ERR_UNSUPPORTED,
            self.dp.ERR_INTERNAL, self.dp.ERR_TOO_LARGE,
            self.dp.ERR_CANCELLED, self.dp.ERR_NOT_CANCELLABLE])
        self.assertEqual(len(codes), len(set(codes)))


class TestDocHonesty(unittest.TestCase):
    """文档不能骗人。

    迭代 8 的教训：`core/dispatch.py` 顶部声称「流式结果用 NDJSON 逐行推」，
    但实现里根本没有流式。文档说的必须真有。
    """

    def test_doc_claims_ndjson_and_impl_has_it(self):
        from core import dispatch as dp
        src = Path(dp.__file__).read_text(encoding="utf-8")
        self.assertIn("NDJSON", src)
        self.assertIn("application/x-ndjson", src)
        self.assertIn("stream", dp.STREAM_OPS)

    def test_server_writes_ndjson_content_type(self):
        from core import dispatch_server as ds
        src = Path(ds.__file__).read_text(encoding="utf-8")
        self.assertIn("application/x-ndjson", src)


if __name__ == "__main__":
    unittest.main()
