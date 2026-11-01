"""core/runs.py —— 洞察运行记录的落盘/列举/回看。

这个模块的价值全在「重启不丢」：GUI 关掉、进程重启、用户隔天再来，
上一轮的分析还在 ~/.mosu/runs/ 里躺着可以回看。

测试重点在**边界**：
  - 路径穿越（run_id 来自 URL query，不校验就是任意文件读）
  - 原子写（并发读不能读到半个 JSON）
  - 上限淘汰（不能把 ~/.mosu 撑大）
  - 损坏文件不拖垮整个列表（一个坏文件不能让历史面板整个报错）
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _RunsTestBase(unittest.TestCase):
    """每个用例用独立的临时 MOSU_HOME，互不干扰。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("MOSU_HOME")
        os.environ["MOSU_HOME"] = self._tmp.name
        # core.runs 是惰性读环境变量的模块级函数，必须重新 import 才生效
        import importlib
        from core import runs as runs_mod
        self.runs = importlib.reload(runs_mod)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MOSU_HOME", None)
        else:
            os.environ["MOSU_HOME"] = self._old
        self._tmp.cleanup()


class TestRunId(unittest.TestCase):
    def test_id_has_timestamp_prefix(self):
        from core.runs import make_run_id
        rid = make_run_id("我什么时候最话多")
        # 20261104-153012-123-<hash8>-<seq5>
        self.assertRegex(rid, r"^\d{8}-\d{6}-\d{3}-[0-9a-f]{8}-\d{5}$")

    def test_same_millisecond_still_gets_distinct_ids(self):
        """毫秒时间戳还不够——同毫秒连问两次也必须不撞名（撞名=覆盖=丢数据）。"""
        from core.runs import make_run_id
        ids = {make_run_id("同一个问题") for _ in range(50)}
        self.assertEqual(len(ids), 50, "50 次生成中有撞名")

    def test_same_question_gets_same_hash_part(self):
        from core.runs import make_run_id
        a = make_run_id("同一个问题").split("-")
        b = make_run_id("同一个问题").split("-")
        # 第 4 段是问题哈希（索引 3：日期-时分秒-毫秒-hash-seq）
        self.assertEqual(a[3], b[3])

    def test_different_question_gets_different_hash(self):
        from core.runs import make_run_id
        a = make_run_id("问题 A").split("-")[-1]
        b = make_run_id("问题 B").split("-")[-1]
        self.assertNotEqual(a, b)

    def test_empty_question_still_generates_valid_id(self):
        from core.runs import make_run_id
        rid = make_run_id("")
        self.assertRegex(rid, r"^\d{8}-\d{6}-\d{3}-[0-9a-f]{8}-\d{5}$")


class TestSaveLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("MOSU_HOME")
        os.environ["MOSU_HOME"] = self._tmp.name
        import importlib
        from core import runs as runs_mod
        self.runs = importlib.reload(runs_mod)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MOSU_HOME", None)
        else:
            os.environ["MOSU_HOME"] = self._old
        self._tmp.cleanup()

    def test_save_then_load_roundtrip(self):
        rid = self.runs.save_run({
            "question": "我什么时候最话多",
            "answer": "晚上 10 点到 11 点。",
            "steps": [{"index": 1, "label": "查时段分布"}],
            "evidence": [{"time": "2026-01-01", "text": "在吗"}],
            "branches": [{"id": "b1", "goal": "查王小红"}],
            "cost": 12.5,
        })
        got = self.runs.load_run(rid)
        self.assertIsNotNone(got)
        self.assertEqual(got["question"], "我什么时候最话多")
        self.assertEqual(got["answer"], "晚上 10 点到 11 点。")
        self.assertEqual(len(got["steps"]), 1)
        self.assertEqual(len(got["evidence"]), 1)
        self.assertEqual(len(got["branches"]), 1)
        self.assertEqual(got["cost"], 12.5)

    def test_save_creates_file_with_expected_name(self):
        rid = self.runs.save_run({"question": "q", "answer": "a"})
        p = self.runs.runs_dir() / f"{rid}.json"
        self.assertTrue(p.is_file())

    def test_save_is_atomic_no_tmp_left_behind(self):
        """临时文件必须被 replace 掉，不能留 .tmp 垃圾在目录里。"""
        self.runs.save_run({"question": "q", "answer": "a"})
        leftovers = list(self.runs.runs_dir().glob("*.tmp"))
        self.assertEqual(leftovers, [], f"残留临时文件：{leftovers}")

    def test_load_missing_returns_none(self):
        self.assertIsNone(self.runs.load_run("20990101-000000-deadbeef"))

    def test_load_rejects_path_traversal(self):
        """run_id 来自 URL query —— 不防穿越就是任意文件读。"""
        for bad in ["../../etc/passwd", "..\\..\\windows\\system32",
                    "a/../../b", ""]:
            self.assertIsNone(self.runs.load_run(bad),
                              f"居然读到了 {bad!r}")

    def test_load_rejects_traversal_without_raising(self):
        self.assertIsNone(self.runs.load_run("../secret"))

    def test_unicode_roundtrip(self):
        rid = self.runs.save_run({
            "question": "跟李小明聊了什么？",
            "answer": "聊了期末考试和火锅 🍲",
        })
        got = self.runs.load_run(rid)
        self.assertEqual(got["answer"], "聊了期末考试和火锅 🍲")

    def test_same_id_does_not_overwrite_existing_record(self):
        """同名不覆盖——覆盖就是丢数据，宁可多留一份带后缀的。

        save_run 返回的才是真正写进去的 id（可能带 -2 后缀）。
        """
        rid1 = self.runs.save_run({"question": "q", "answer": "第一版"})
        rid2 = self.runs.save_run({"id": rid1, "question": "q", "answer": "第二版"})
        self.assertNotEqual(rid1, rid2, "第二次写居然用了同一个 id")
        self.assertEqual(self.runs.load_run(rid1)["answer"], "第一版",
                         "旧记录被覆盖了")
        self.assertEqual(self.runs.load_run(rid2)["answer"], "第二版")

    def test_saved_at_is_stamped(self):
        rid = self.runs.save_run({"question": "q", "answer": "a"})
        self.assertIn("savedAt", self.runs.load_run(rid))


class TestListRuns(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("MOSU_HOME")
        os.environ["MOSU_HOME"] = self._tmp.name
        import importlib
        from core import runs as runs_mod
        self.runs = importlib.reload(runs_mod)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MOSU_HOME", None)
        else:
            os.environ["MOSU_HOME"] = self._old
        self._tmp.cleanup()

    def test_empty_dir_returns_empty_list(self):
        self.assertEqual(self.runs.list_runs(), [])

    def test_list_returns_summaries_not_full_records(self):
        """列表不能带全文——evidence 原文很大，全量返回会拖垮历史面板。"""
        self.runs.save_run({
            "question": "问题" * 50,
            "answer": "答案" * 200,
            "evidence": [{"text": "原话" * 100}],
        })
        items = self.runs.list_runs()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertNotIn("evidence", it)
        self.assertNotIn("steps", it)
        self.assertLessEqual(len(it["question"]), 120)
        self.assertLessEqual(len(it["answerHead"]), 160)

    def test_summary_count_keys_are_suffixed(self):
        """计数字段必须带 Count 后缀，避免与详情里的同名数组字段混淆。"""
        self.runs.save_run({
            "question": "q", "answer": "a",
            "steps": [1, 2, 3], "evidence": [1], "branches": [1, 2],
        })
        it = self.runs.list_runs()[0]
        self.assertEqual(it["stepsCount"], 3)
        self.assertEqual(it["evidenceCount"], 1)
        self.assertEqual(it["branchesCount"], 2)

    def test_list_is_newest_first(self):
        for i in range(3):
            self.runs.save_run({"question": f"q{i}", "answer": "a"})
        items = self.runs.list_runs()
        self.assertEqual(items[0]["question"], "q2")
        self.assertEqual(items[-1]["question"], "q0")

    def test_list_respects_limit(self):
        for i in range(5):
            self.runs.save_run({"question": f"q{i}", "answer": "a"})
        self.assertEqual(len(self.runs.list_runs(limit=2)), 2)

    def test_corrupt_file_does_not_break_whole_list(self):
        """一个坏文件不能让历史面板整个报错——跳过它，其余照常显示。"""
        self.runs.save_run({"question": "好的", "answer": "a"})
        bad = self.runs.runs_dir() / "20990101-000000-badbadba.json"
        bad.write_text("{这不是 JSON", encoding="utf-8")
        items = self.runs.list_runs()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["question"], "好的")

    def test_trim_keeps_newest_and_drops_oldest(self):
        for i in range(60):
            self.runs.save_run({"question": f"q{i}", "answer": "a"})
        files = sorted(self.runs.runs_dir().glob("*.json"))
        self.assertLessEqual(len(files), 50,
                             f"超出上限没清理：{len(files)} 个")
        # 最新的必须还在
        items = self.runs.list_runs()
        self.assertEqual(items[0]["question"], "q59")


class TestDeleteRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("MOSU_HOME")
        os.environ["MOSU_HOME"] = self._tmp.name
        import importlib
        from core import runs as runs_mod
        self.runs = importlib.reload(runs_mod)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MOSU_HOME", None)
        else:
            os.environ["MOSU_HOME"] = self._old
        self._tmp.cleanup()

    def test_delete_removes_file(self):
        rid = self.runs.save_run({"question": "q", "answer": "a"})
        self.assertTrue(self.runs.delete_run(rid))
        self.assertIsNone(self.runs.load_run(rid))

    def test_delete_missing_returns_false(self):
        self.assertFalse(self.runs.delete_run("20990101-000000-deadbeef"))

    def test_delete_rejects_traversal(self):
        self.assertFalse(self.runs.delete_run("../../important.txt"))


class TestTaskHubEvents(unittest.TestCase):
    """TaskHub 的事件机制：SSE 能按 seq 增量拿到事件，且不丢开头。"""

    def setUp(self):
        from gui.server import _TaskHub
        import tempfile as _tf
        self._tmp = _tf.TemporaryDirectory()
        self.hub = _TaskHub(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_emit_appends_with_monotonic_seq(self):
        tid = self.hub.submit("t", lambda tid: None)
        self.hub.emit(tid, "step", step={"index": 1})
        self.hub.emit(tid, "step", step={"index": 2})
        evs, seq, _ = self.hub.wait_events(tid, 0, timeout=0.05)
        self.assertEqual([e["seq"] for e in evs], [1, 2])
        self.assertEqual(seq, 2)

    def test_wait_events_only_returns_after_seq(self):
        tid = self.hub.submit("t", lambda tid: None)
        self.hub.emit(tid, "step", n=1)
        self.hub.emit(tid, "step", n=2)
        evs, _, _ = self.hub.wait_events(tid, 1, timeout=0.05)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["n"], 2)

    def test_wait_events_timeout_returns_empty(self):
        tid = self.hub.submit("t", lambda tid: None)
        evs, seq, snap = self.hub.wait_events(tid, 0, timeout=0.05)
        self.assertEqual(evs, [])
        self.assertEqual(seq, 0)
        self.assertIsNotNone(snap)

    def test_snapshot_has_live_and_status(self):
        tid = self.hub.submit("t", lambda tid: None)
        self.hub.report(tid, steps=[{"index": 1}])
        snap = self.hub.snapshot(tid)
        self.assertEqual(snap["status"], "queued")
        self.assertEqual(len(snap["live"]["steps"]), 1)

    def test_emit_on_unknown_task_is_noop(self):
        self.hub.emit("nonexistent", "step")   # 不能抛

    def test_status_transitions_to_done(self):
        import time
        tid = self.hub.submit("t", lambda tid: {"ok": True})
        for _ in range(50):
            snap = self.hub.snapshot(tid)
            if snap and snap["status"] == "done":
                break
            time.sleep(0.02)
        snap = self.hub.snapshot(tid)
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["result"], {"ok": True})
        self.assertIsNotNone(snap["cost"])

    def test_failed_task_records_error(self):
        import time

        def boom(tid):
            raise RuntimeError("炸了")
        tid = self.hub.submit("t", boom)
        for _ in range(50):
            snap = self.hub.snapshot(tid)
            if snap and snap["status"] == "error":
                break
            time.sleep(0.02)
        snap = self.hub.snapshot(tid)
        self.assertEqual(snap["status"], "error")
        self.assertIn("炸了", snap["error"])

    def _bare_task(self):
        """直接塞一个任务，不经过 submit —— 这样**没有任何其他 notify 源**。

        为什么要绕开 submit：submit 会启动 worker 线程，worker 进循环时
        会 status="running" + notify_all()。那个通知可能正好落在等待窗口里，
        把等待者提前唤醒 → 测试假阴性（bug 在但测试是绿的）。
        测「丢失唤醒」必须排除一切干扰通知源。
        """
        tid = "bare-test-task"
        with self.hub._lock:
            self.hub._tasks[tid] = {"id": tid, "kind": "t", "status": "running",
                                    "cost": None, "result": None, "error": None,
                                    "live": {}, "meta": {}}
            self.hub._events[tid] = []
            self.hub._seq[tid] = 0
        return tid

    def test_wait_events_does_not_lose_wakeup(self):
        """回归：事件在「取快照」与「进入等待」之间到达时，不能白等满 timeout。

        早先的写法是「直接 wait(timeout) 再取事件」，notify 发给空气，
        订阅方白等一整个 timeout —— 实测每个事件都被延迟 1 秒。
        正确写法：先查条件，不满足才等；醒来后重新查。
        """
        import time
        tid = self._bare_task()

        # 模拟「事件已经到达，但订阅方还没进入等待」的窗口
        self.hub.emit(tid, "step", n=1)
        t0 = time.time()
        evs, seq, _ = self.hub.wait_events(tid, 0, timeout=1.0)
        elapsed = time.time() - t0
        self.assertEqual(len(evs), 1, "事件被漏掉了")
        self.assertLess(elapsed, 0.3,
                        f"白等了 {elapsed:.2f}s —— 丢失唤醒没修好")

    def test_wait_events_wakes_on_new_event(self):
        """事件在等待期间到达 → 立刻被唤醒，不用等满 timeout。"""
        import threading
        import time
        tid = self._bare_task()
        result = {}

        def waiter():
            t0 = time.time()
            evs, _, _ = self.hub.wait_events(tid, 0, timeout=2.0)
            result["n"] = len(evs)
            result["dt"] = time.time() - t0

        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.15)
        self.hub.emit(tid, "step", n=1)
        th.join(timeout=3)
        self.assertEqual(result.get("n"), 1)
        self.assertLess(result.get("dt", 9), 1.0,
                        "等了太久才被唤醒——事件推送不是即时的")

    def test_meta_is_stored(self):
        tid = self.hub.submit("t", lambda tid: None, meta={"question": "问题"})
        self.assertEqual(self.hub.snapshot(tid)["meta"]["question"], "问题")

    def test_worker_survives_task_exception(self):
        """worker 抛异常不能整个死掉——后面排队的任务还得能跑。"""
        import time

        def boom(tid):
            raise RuntimeError("x")

        def ok(tid):
            return 1
        t1 = self.hub.submit("t", boom)
        t2 = self.hub.submit("t", ok)
        for _ in range(80):
            s2 = self.hub.snapshot(t2)
            if s2 and s2["status"] == "done":
                break
            time.sleep(0.02)
        self.assertEqual(self.hub.snapshot(t2)["status"], "done")
        self.assertEqual(self.hub.snapshot(t1)["status"], "error")


if __name__ == "__main__":
    unittest.main()
