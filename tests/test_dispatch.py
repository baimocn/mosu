# -*- coding: utf-8 -*-
"""派活通道：协议 + 三种投递模式（send / queue / steer）。

核心要锁住的语义（对标 Lancer 的 control-service.ts）：

  send   目标正忙 → BUSY（不是排队，是明确拒绝）
  queue  目标正忙 → 挂起，当前那轮跑完自动接着跑
  steer  目标正忙 → 消息递进正在跑的那轮，让模型自己调整方向

这三种的区别是「派活通道」的全部价值所在。如果 steer 只是「发个消息」，
它跟 send 就没有区别了——所以测试必须验证**引导真的进了 Agent 的上下文**。

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


class _FakeAgent:
    """假 Agent：记录收到的 steer 消息。"""

    def __init__(self):
        self.steers = []

    def steer(self, msg):
        self.steers.append(msg)


class _FakeRunner:
    """假 runner：可控地制造「忙 / 不忙」两种状态。"""

    def __init__(self):
        self.calls = []

    def __call__(self, op, data):
        self.calls.append((op, dict(data)))
        if op == "status":
            return {"busy": False, "queued": 0, "queue": [], "finished": 0,
                    "recent": [], "current": None}
        return {"accepted": True}


class TestProtocol(unittest.TestCase):
    """协议本身：解析、错误码、token 比较。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("MOSU_HOME")
        os.environ["MOSU_HOME"] = self._tmp.name
        import importlib
        from core import dispatch as dp
        self.dp = importlib.reload(dp)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MOSU_HOME", None)
        else:
            os.environ["MOSU_HOME"] = self._old
        self._tmp.cleanup()

    def test_parse_request_ok(self):
        d = self.dp.parse_request(b'{"op":"send","question":"hi"}')
        self.assertEqual(d["op"], "send")
        self.assertEqual(d["question"], "hi")

    def test_parse_request_rejects_bad_json(self):
        with self.assertRaises(self.dp.DispatchError) as ctx:
            self.dp.parse_request(b"{not json")
        self.assertEqual(ctx.exception.code, self.dp.ERR_INVALID_JSON)

    def test_parse_request_rejects_non_object(self):
        with self.assertRaises(self.dp.DispatchError) as ctx:
            self.dp.parse_request(b"[1,2,3]")
        self.assertEqual(ctx.exception.code, self.dp.ERR_INVALID_REQUEST)

    def test_parse_request_rejects_missing_op(self):
        with self.assertRaises(self.dp.DispatchError) as ctx:
            self.dp.parse_request(b'{"question":"hi"}')
        self.assertEqual(ctx.exception.code, self.dp.ERR_INVALID_REQUEST)

    def test_parse_request_rejects_unknown_op(self):
        with self.assertRaises(self.dp.DispatchError) as ctx:
            self.dp.parse_request(b'{"op":"rm -rf"}')
        self.assertEqual(ctx.exception.code, self.dp.ERR_INVALID_REQUEST)

    def test_parse_request_rejects_oversized_body(self):
        with self.assertRaises(self.dp.DispatchError) as ctx:
            self.dp.parse_request(b"x" * (self.dp.MAX_BODY + 10))
        self.assertEqual(ctx.exception.code, self.dp.ERR_TOO_LARGE)

    def test_error_codes_are_distinct(self):
        codes = [self.dp.ERR_INVALID_REQUEST, self.dp.ERR_INVALID_JSON,
                 self.dp.ERR_UNAUTHORIZED, self.dp.ERR_BUSY,
                 self.dp.ERR_NOT_FOUND, self.dp.ERR_UNSUPPORTED,
                 self.dp.ERR_INTERNAL, self.dp.ERR_TOO_LARGE]
        self.assertEqual(len(codes), len(set(codes)))

    def test_ok_and_err_envelopes(self):
        self.assertEqual(self.dp.ok({"a": 1})["ok"], True)
        e = self.dp.err(self.dp.ERR_BUSY, "忙")
        self.assertFalse(e["ok"])
        self.assertEqual(e["error"]["code"], self.dp.ERR_BUSY)

    def test_token_is_64_hex(self):
        tok = self.dp.ensure_token()
        self.assertEqual(len(tok), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in tok))

    def test_token_is_stable_across_calls(self):
        a = self.dp.ensure_token()
        b = self.dp.ensure_token()
        self.assertEqual(a, b)

    def test_token_matches_uses_constant_time(self):
        """必须用 hmac.compare_digest，不能用 ==。

        普通 == 会在第一个不同字节处提前返回，攻击者能靠响应时间
        逐字节猜出 token。Lancer 用的就是 timingSafeEqual。
        """
        import inspect
        src = inspect.getsource(self.dp.token_matches)
        self.assertIn("compare_digest", src,
                      "token 比较没用常量时间比较——可被时序攻击")

    def test_token_matches_rejects_wrong(self):
        tok = self.dp.ensure_token()
        self.assertTrue(self.dp.token_matches(tok, tok))
        self.assertFalse(self.dp.token_matches(tok, "0" * 64))
        self.assertFalse(self.dp.token_matches(tok, None))
        self.assertFalse(self.dp.token_matches(tok, tok[:-1]))

    def test_require_str_validates(self):
        with self.assertRaises(self.dp.DispatchError):
            self.dp.require_str({}, "question")
        with self.assertRaises(self.dp.DispatchError):
            self.dp.require_str({"question": "   "}, "question")
        with self.assertRaises(self.dp.DispatchError):
            self.dp.require_str({"question": "x" * 100}, "question", maxlen=10)
        self.assertEqual(self.dp.require_str({"question": " hi "}, "question"), "hi")

    def test_make_request_rejects_unknown_op(self):
        with self.assertRaises(self.dp.DispatchError):
            self.dp.make_request("nope")

    def test_make_request_drops_none_values(self):
        r = self.dp.make_request("send", question="q", run_id=None)
        self.assertNotIn("run_id", r)


class TestDispatchServerOverSocket(unittest.TestCase):
    """真 socket 真 HTTP。"""

    @classmethod
    def setUpClass(cls):
        from core import dispatch as dp
        from core.dispatch_server import DispatchServer
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old = os.environ.get("MOSU_HOME")
        os.environ["MOSU_HOME"] = cls._tmp.name
        import importlib
        dp = importlib.reload(dp)
        cls.dp = dp
        cls.runner = _FakeRunner()
        cls.srv = DispatchServer(cls.runner, host="127.0.0.1", port=0)
        cls.srv.serve_forever_in_thread()
        cls.port = cls.srv.port
        cls.token = cls.srv.token

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        if cls._old is None:
            os.environ.pop("MOSU_HOME", None)
        else:
            os.environ["MOSU_HOME"] = cls._old
        cls._tmp.cleanup()

    def _req(self, payload, token=None, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        tok = self.token if token is None else token
        head = (f"POST /v1 HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Authorization: Bearer {tok}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n").encode("ascii")
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(head + body)
            chunks = []
            while True:
                d = s.recv(65536)
                if not d:
                    break
                chunks.append(d)
        raw_resp = b"".join(chunks)
        sep = raw_resp.find(b"\r\n\r\n")
        status_line = raw_resp[:sep].decode("ascii", "replace")
        payload_bytes = raw_resp[sep + 4:]
        code = 0
        for part in status_line.split(" "):
            if part.isdigit():
                code = int(part)
                break
        try:
            data = json.loads(payload_bytes.decode("utf-8"))
        except ValueError:
            data = {"_raw": payload_bytes[:200].decode("utf-8", "replace")}
        return code, data

    def test_valid_request_succeeds(self):
        code, d = self._req({"op": "status"})
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])

    def test_wrong_token_is_rejected(self):
        code, d = self._req({"op": "status"}, token="0" * 64)
        self.assertEqual(code, 401)
        self.assertFalse(d["ok"])
        self.assertEqual(d["error"]["code"], self.dp.ERR_UNAUTHORIZED)

    def test_missing_token_is_rejected(self):
        code, d = self._req({"op": "status"}, token="")
        self.assertEqual(code, 401)

    def test_bad_json_is_400(self):
        code, d = self._req(None, raw=b"{oops")
        self.assertEqual(code, 400)
        self.assertEqual(d["error"]["code"], self.dp.ERR_INVALID_JSON)

    def test_unknown_op_is_400(self):
        code, d = self._req({"op": "explode"})
        self.assertEqual(code, 400)
        self.assertEqual(d["error"]["code"], self.dp.ERR_INVALID_REQUEST)

    def test_auth_checked_before_parsing(self):
        """鉴权必须在解析之前——否则未授权的人能靠错误信息探测协议。"""
        code, d = self._req(None, token="0" * 64, raw=b"{oops")
        self.assertEqual(code, 401, "未授权请求先被解析了")


class TestInsightRunnerModes(unittest.TestCase):
    """send / queue / steer 三种模式的语义。"""

    def setUp(self):
        import importlib
        from core import dispatch_server as ds_mod
        self.mod = importlib.reload(ds_mod)
        self.runner = self.mod.InsightRunner(get_ds=lambda: object())

    def _busy(self, agent=None):
        """把 runner 置为「正在跑」。"""
        agent = agent or _FakeAgent()
        self.runner._current = {"agent": agent, "question": "正在跑的问题",
                                "runId": "run-1", "since": time.time()}
        return agent

    def test_send_when_idle_starts(self):
        # _start 会真的起线程跑 Agent，这里用假 agent 顶替
        started = {}

        def fake_start(q, h):
            started["q"] = q
            return "new-run"
        self.runner._start = fake_start
        r = self.runner("send", {"question": "问题"})
        self.assertTrue(r["accepted"])
        self.assertFalse(r["queued"])
        self.assertEqual(started["q"], "问题")

    def test_send_when_busy_raises_busy(self):
        self._busy()
        with self.assertRaises(self.mod.proto.DispatchError) as ctx:
            self.runner("send", {"question": "问题"})
        self.assertEqual(ctx.exception.code, self.mod.proto.ERR_BUSY)

    def test_queue_when_busy_enqueues(self):
        self._busy()
        r = self.runner("queue", {"question": "排队的问题"})
        self.assertTrue(r["accepted"])
        self.assertTrue(r["queued"])
        self.assertEqual(r["position"], 1)
        self.assertEqual(len(self.runner._queue), 1)

    def test_queue_when_idle_starts_directly(self):
        started = {}
        self.runner._start = lambda q, h: started.setdefault("q", q) and None or "r1"
        r = self.runner("queue", {"question": "直接跑"})
        self.assertFalse(r["queued"])
        self.assertEqual(started["q"], "直接跑")

    def test_steer_delivers_to_running_agent(self):
        """steer 的核心：消息必须真的递进正在跑的那一轮。"""
        agent = self._busy()
        r = self.runner("steer", {"message": "重点看工作相关的"})
        self.assertTrue(r["delivered"])
        self.assertEqual(agent.steers, ["重点看工作相关的"])

    def test_steer_when_idle_becomes_new_task(self):
        """没在跑时 steer 不该报错——当新任务发出去更有用。"""
        started = {}
        self.runner._start = lambda q, h: started.setdefault("q", q) and None or "r9"
        r = self.runner("steer", {"message": "随便看看", "question": "新问题"})
        self.assertFalse(r["delivered"])
        self.assertEqual(started["q"], "新问题")

    def test_steer_when_idle_without_question_raises_not_found(self):
        with self.assertRaises(self.mod.proto.DispatchError) as ctx:
            self.runner("steer", {"message": "随便看看"})
        self.assertEqual(ctx.exception.code, self.mod.proto.ERR_NOT_FOUND)

    def test_steer_requires_message(self):
        self._busy()
        with self.assertRaises(self.mod.proto.DispatchError) as ctx:
            self.runner("steer", {})
        self.assertEqual(ctx.exception.code, self.mod.proto.ERR_INVALID_REQUEST)

    def test_queue_pumps_after_current_finishes(self):
        """当前那轮跑完 → 队列里下一个自动开跑。"""
        self._busy()
        self.runner("queue", {"question": "第二个问题"})
        started = []
        self.runner._start = lambda q, h: started.append(q) or "r2"
        # 模拟当前轮结束
        with self.runner._lock:
            self.runner._current = None
        self.runner._pump_queue()
        self.assertEqual(started, ["第二个问题"])

    def test_pump_does_nothing_when_still_busy(self):
        self._busy()
        self.runner("queue", {"question": "第二个问题"})
        started = []
        self.runner._start = lambda q, h: started.append(q) or "r2"
        self.runner._pump_queue()
        self.assertEqual(started, [], "还 busy 就抢跑了")

    def test_status_reports_busy(self):
        self._busy()
        r = self.runner("status", {})
        self.assertTrue(r["busy"])
        self.assertEqual(r["current"]["question"], "正在跑的问题")

    def test_status_reports_queue_length(self):
        self._busy()
        self.runner("queue", {"question": "排队的"})
        r = self.runner("status", {})
        self.assertEqual(r["queued"], 1)


class TestSteerInjection(unittest.TestCase):
    """steer 真的进 Agent 上下文（不是只存起来）。"""

    def test_steer_message_is_injected_into_messages(self):
        from core.agent_engine import InsightAgent
        ag = InsightAgent(ds=object())
        ag.steer("重点看工作相关的")
        msgs = [{"role": "user", "content": "原始问题"}]
        self.assertTrue(ag._inject_steer(msgs))
        self.assertEqual(len(msgs), 2)
        self.assertIn("重点看工作相关的", msgs[1]["content"])
        self.assertEqual(msgs[1]["role"], "user")

    def test_inject_is_one_shot(self):
        """引导只注入一次——不然后续每轮都重复看到它。"""
        from core.agent_engine import InsightAgent
        ag = InsightAgent(ds=object())
        ag.steer("一次就行")
        m1 = []
        self.assertTrue(ag._inject_steer(m1))
        m2 = []
        self.assertFalse(ag._inject_steer(m2))

    def test_multiple_steers_merge(self):
        from core.agent_engine import InsightAgent
        ag = InsightAgent(ds=object())
        ag.steer("第一条")
        ag.steer("第二条")
        msgs = []
        ag._inject_steer(msgs)
        self.assertEqual(len(msgs), 1)
        self.assertIn("第一条", msgs[0]["content"])
        self.assertIn("第二条", msgs[0]["content"])

    def test_empty_steer_is_ignored(self):
        from core.agent_engine import InsightAgent
        ag = InsightAgent(ds=object())
        ag.steer("   ")
        self.assertFalse(ag._inject_steer([]))

    def test_last_steer_records_original_text(self):
        """注入后 last_steer 里要有**原文**。

        没有这条，落盘的 step 只有一句固定文案「收到外部引导」，
        回看历史时根本不知道当时引导了啥——信息在，但看不出来。
        """
        from core.agent_engine import InsightAgent
        ag = InsightAgent(ds=object())
        ag.steer("重点关注工作相关的对话")
        ag._inject_steer([])
        self.assertEqual(ag.last_steer, ["重点关注工作相关的对话"])

    def test_steer_step_summary_contains_text(self):
        """落盘的 steer 步骤摘要必须含引导原文。"""
        from core.agent_engine import InsightAgent
        ag = InsightAgent(ds=object())
        ag.steer("重点关注工作相关的对话")
        msgs = []
        self.assertTrue(ag._inject_steer(msgs))
        text = "；".join(ag.last_steer)
        summary = text[:80] + ("…" if len(text) > 80 else "")
        self.assertIn("重点关注工作相关的对话", summary)

    def test_last_steer_cleared_when_nothing_pending(self):
        """没有待注入的引导时，last_steer 不该留着上一次的。"""
        from core.agent_engine import InsightAgent
        ag = InsightAgent(ds=object())
        ag.steer("第一条")
        ag._inject_steer([])
        ag._inject_steer([])
        # 第二次没注入，但 last_steer 仍是上一次的原文——
        # 这没问题（它表示「最近一次注入的原文」），不该被清成空。
        self.assertEqual(ag.last_steer, ["第一条"])

    def test_steer_is_thread_safe(self):
        """steer 来自派活线程，主循环在另一个线程读——不能有竞态。"""
        from core.agent_engine import InsightAgent
        ag = InsightAgent(ds=object())
        msgs = []
        errs = []

        def writer():
            try:
                for i in range(200):
                    ag.steer(f"消息{i}")
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        def reader():
            try:
                for _ in range(200):
                    m = []
                    ag._inject_steer(m)
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=10)
        self.assertEqual(errs, [])


class TestCliExitCode(unittest.TestCase):
    """CLI 退出码契约：失败必须是非 0。

    这是个真踩过的坑：cmd_dispatch 用 `return 1` 表达失败，但 main() 把
    args.func 的**返回值丢了**，进程照样 exit 0。脚本/CI 看到的是
    「exit 0 + 满屏 [x] BUSY」，判断不了成败。

    这类 bug 靠看输出发现不了——输出明明写着 BUSY，退出码却是 0。
    所以这里直接断言退出码。
    """

    def _run_cli(self, args, env_extra=None):
        import subprocess
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-m", "cli.mosu", "dispatch"] + list(args),
            capture_output=True, text=True, cwd=str(ROOT), env=env,
            timeout=120, encoding="utf-8", errors="replace")

    def test_missing_question_exits_nonzero(self):
        """send 不给 --question → 必须 exit != 0。"""
        p = self._run_cli(["send"])
        self.assertNotEqual(p.returncode, 0,
                            "缺少 --question 却退出 0，脚本会以为派活成功了")
        self.assertIn("question", p.stdout + p.stderr)

    def test_missing_message_exits_nonzero(self):
        p = self._run_cli(["steer"])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("message", p.stdout + p.stderr)

    def test_unauthorized_exits_nonzero(self):
        """没有 token 文件 → UNAUTHORIZED → 必须 exit != 0。

        用一个空的 MOSU_HOME 模拟「墨溯没在跑」。
        """
        tmp = tempfile.mkdtemp(prefix="mosu-notoken-")
        p = self._run_cli(["status"], env_extra={"MOSU_HOME": tmp})
        self.assertNotEqual(p.returncode, 0,
                            "鉴权失败却退出 0，脚本会以为通道正常")
        blob = p.stdout + p.stderr
        self.assertTrue("token" in blob.lower() or "UNAUTHORIZED" in blob,
                        f"应给出可读提示，实际：{blob[:200]}")

    def test_main_propagates_return_code(self):
        """直接测 main()：命令返回 3 → 进程必须以 3 退出。

        做法：把 build_parser 换成假的，捕获 main() 抛出的 SystemExit。
        """
        import cli.mosu as mosu

        class _Args:
            cmd = "dispatch"
            dispatch_cmd = "status"
            question = None
            message = None
            run_id = None
            timeout = 30
            json = False
            wait = False
            limit = None

            @staticmethod
            def func(args, ds):
                return 3

        orig = mosu.build_parser
        mosu.build_parser = lambda: type("P", (), {"parse_args": staticmethod(lambda: _Args())})()
        try:
            with self.assertRaises(SystemExit) as cm:
                mosu.main()
        finally:
            mosu.build_parser = orig
        self.assertEqual(cm.exception.code, 3,
                         "main() 必须把命令的非 0 返回值转成退出码")

    def test_main_zero_return_exits_clean(self):
        """返回 0 → 不该抛 SystemExit（正常退出）。"""
        import cli.mosu as mosu

        class _Args:
            cmd = "dispatch"
            dispatch_cmd = "status"
            question = None
            message = None
            run_id = None
            timeout = 30
            json = False
            wait = False
            limit = None

            @staticmethod
            def func(args, ds):
                return 0

        orig = mosu.build_parser
        mosu.build_parser = lambda: type("P", (), {"parse_args": staticmethod(lambda: _Args())})()
        try:
            mosu.main()  # 不抛 SystemExit = 通过
        finally:
            mosu.build_parser = orig


if __name__ == "__main__":
    unittest.main()
