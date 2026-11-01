# -*- coding: utf-8 -*-
"""多代理编排层（core/agent_team.py）的离线测试。

不调真实 LLM、不碰真实库——用假 LLM 客户端替身。

覆盖的关键点（每一条都是踩过的坑或 Lancer 的核心纪律）：
  - 预算准入：reserve 在起线程前占位，防止并发超订
  - 深度上限：子代理拿不到 delegate_tasks（否则无限递归）
  - 回执状态机：跑完是 reported，父级核对才 accepted
  - 数字审计：子代理的数字必须进父代理的审计范围
  - 有界回传：子代理结论不能撑爆父代理上下文
  - 并行真并行：多个子代理的 LLM 推理同时进行
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agent_engine import DELEGATE_SCHEMA, InsightAgent
from core.agent_team import (AgentTeam, BranchBudget, branch_payload,
                              settle_children)
from core.agent_tools import ToolBox


class _FakeLlm:
    """假 LLM：按预设脚本返回工具调用或文本。

    script: list of dict，每项形如
      {"calls": [{"id": "c1", "name": "chat_stats", "args": {...}}]}
      或 {"text": "最终答案"}
    """

    def __init__(self, script=None, delay=0.0):
        self.script = list(script or [])
        self.delay = delay
        self.calls_made = []
        self.model = "fake-model"

    def chat_tools(self, messages, tools, temperature=0.3, max_tokens=1500,
                   tool_choice="auto"):
        if self.delay:
            time.sleep(self.delay)
        self.calls_made.append({
            "n_tools": len(tools),
            "tool_names": [t["function"]["name"] for t in tools],
            "n_messages": len(messages),
        })
        if self.script:
            item = self.script.pop(0)
        else:
            item = {"text": "（脚本耗尽）"}
        if "text" in item:
            return {"content": item["text"], "tool_calls": [],
                    "usage": {"total_tokens": 10}}
        return {"content": None, "tool_calls": item.get("calls") or [],
                "usage": {"total_tokens": 10}}


class _FakeDs:
    """假数据集：只提供 ToolBox 用到的几个方法。"""

    def __init__(self):
        self.calls = []

    def sessions(self):
        return []

    def find_session(self, q):
        return None

    def find_sessions(self, q):
        return []


# ------------------------------------------------------------------ 预算

class TestBranchBudget(unittest.TestCase):
    """树级预算：多代理最常见的翻车是「查太多」而不是「查错」。"""

    def test_reserve_within_limit(self):
        b = BranchBudget({"max_branches": 2})
        self.assertIsNone(b.reserve(2))
        self.assertIsNotNone(b.reserve(1), "超限必须被拒绝")

    def test_release_frees_slot(self):
        b = BranchBudget({"max_branches": 1})
        self.assertIsNone(b.reserve(1))
        b.release(1)
        self.assertIsNone(b.reserve(1), "释放后应能再占")

    def test_release_never_negative(self):
        b = BranchBudget()
        b.release(99)
        self.assertIsNone(b.reserve(1))

    def test_step_pool_shared(self):
        b = BranchBudget({"max_steps": 3})
        self.assertTrue(b.try_step())
        self.assertTrue(b.try_step())
        self.assertTrue(b.try_step())
        self.assertFalse(b.try_step(), "步骤池用尽必须拒绝")
        self.assertEqual(b.steps_used, 3)

    def test_defaults_are_sane(self):
        b = BranchBudget()
        self.assertGreaterEqual(b.max_depth, 1)
        self.assertGreaterEqual(b.max_concurrent, 1)
        self.assertLessEqual(b.max_branches, 32, "分支上限太大会烧钱")


# ------------------------------------------------------------------ 委派

class TestDelegate(unittest.TestCase):
    def setUp(self):
        self.ds = _FakeDs()
        self.team = AgentTeam(self.ds, llm=_FakeLlm(), llm_factory=_FakeLlm)

    def test_delegates_each_goal(self):
        brs = self.team.delegate(["查张三", "查李四"], depth=0)
        self.assertEqual(len(brs), 2)
        self.assertEqual([b.goal for b in brs], ["查张三", "查李四"])

    def test_depth_limit_blocks_delegate(self):
        team = AgentTeam(self.ds, llm=_FakeLlm(), llm_factory=_FakeLlm,
                         budget=BranchBudget({"max_depth": 1}))
        self.assertEqual(team.delegate(["x"], depth=1), [],
                         "超过深度上限不应派发")

    def test_can_delegate_reports_reason(self):
        team = AgentTeam(self.ds, llm=_FakeLlm(), llm_factory=_FakeLlm,
                         budget=BranchBudget({"max_depth": 1}))
        ok, why = team.can_delegate(1)
        self.assertFalse(ok)
        self.assertIn("深度", why)

    def test_branch_limit_blocks(self):
        team = AgentTeam(self.ds, llm=_FakeLlm(), llm_factory=_FakeLlm,
                         budget=BranchBudget({"max_branches": 1, "max_concurrent": 4}))
        brs = team.delegate(["a", "b"], depth=0)
        # 一次派 2 个超过 max_branches=1 → 整体被拒
        self.assertEqual(brs, [])

    def test_empty_goals_noop(self):
        self.assertEqual(self.team.delegate([], depth=0), [])
        self.assertEqual(self.team.delegate([None, "  "], depth=0), [])

    def test_results_in_input_order(self):
        """顺序必须按输入，不是按完成快慢——否则父代理会误判重要性。"""
        brs = self.team.delegate(["a", "b", "c"], depth=0)
        self.assertEqual([b.goal for b in brs], ["a", "b", "c"])

    def test_branch_records_parent_child(self):
        brs = self.team.delegate(["a", "b"], depth=0)
        for b in brs:
            self.assertIsNone(b.parent_id)
            self.assertEqual(b.depth, 1)
            self.assertEqual(b.status, "complete")
            self.assertEqual(b.receipt, "reported",
                             "跑完只是 reported，父级核对后才 accepted")

    def test_tree_lists_root_branches(self):
        self.team.delegate(["a"], depth=0)
        tree = self.team.tree()
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0]["goal"], "a")

    def test_failed_branch_does_not_crash_tree(self):
        """子代理炸了不能带崩整棵树。"""
        class BoomLlm:
            model = "boom"

            def chat_tools(self, *a, **kw):
                raise RuntimeError("网关炸了")

        team = AgentTeam(self.ds, llm=BoomLlm(), llm_factory=BoomLlm)
        br = team.run_branch("查一下", depth=1)
        self.assertEqual(br.status, "failed")
        self.assertIn("网关炸了", br.error)
        self.assertTrue(br.report)


# ------------------------------------------------------------------ 回执

class TestReceiptProtocol(unittest.TestCase):
    """回执状态机：子分支跑完 ≠ 结论可用。"""

    def test_reported_by_default(self):
        team = AgentTeam(_FakeDs(), llm=_FakeLlm(), llm_factory=_FakeLlm)
        br = team.run_branch("x", depth=1)
        self.assertEqual(br.receipt, "reported")

    def test_settle_requires_terminal_state(self):
        team = AgentTeam(_FakeDs(), llm=_FakeLlm(), llm_factory=_FakeLlm)
        brs = team.delegate(["a"], depth=0)
        self.assertTrue(settle_children(brs))

    def test_settle_false_when_running(self):
        team = AgentTeam(_FakeDs(), llm=_FakeLlm(), llm_factory=_FakeLlm)
        br = team.run_branch("a", depth=1)
        br.status = "running"
        self.assertFalse(settle_children([br]))

    def test_settle_true_when_failed(self):
        """失败的分支也算结算——不能让一个失败分支永远卡住父代理。"""
        team = AgentTeam(_FakeDs(), llm=_FakeLlm(), llm_factory=_FakeLlm)
        br = team.run_branch("a", depth=1)
        br.status = "failed"
        self.assertTrue(settle_children([br]))


# ------------------------------------------------------------------ 有界回传

class TestBranchPayload(unittest.TestCase):
    def test_payload_is_bounded(self):
        team = AgentTeam(_FakeDs(), llm=_FakeLlm(), llm_factory=_FakeLlm)
        brs = team.delegate(["a"], depth=0)
        txt = json.dumps(branch_payload(brs), ensure_ascii=False)
        self.assertLess(len(txt), 20000, "回传必须有界，否则父代理上下文会爆")

    def test_payload_has_no_raw_tool_dump(self):
        """回传只给结论和少量依据，不给子代理的全部工具返回。"""
        team = AgentTeam(_FakeDs(), llm=_FakeLlm(), llm_factory=_FakeLlm)
        brs = team.delegate(["a"], depth=0)
        payload = branch_payload(brs)
        self.assertIn("分支结果", payload)
        self.assertIn("结论", payload["分支结果"][0])

    def test_payload_evidence_capped(self):
        team = AgentTeam(_FakeDs(), llm=_FakeLlm(), llm_factory=_FakeLlm)
        br = team.run_branch("x", depth=1)
        br.evidence = [{"time": "t", "sender": "s", "text": "m"}] * 50
        payload = branch_payload([br])
        self.assertLessEqual(len(payload["分支结果"][0]["依据"]), 3)


# ------------------------------------------------------------------ 工具隔离

class TestDelegateToolIsolation(unittest.TestCase):
    """最关键的安全属性：子代理不能无限派生。"""

    def test_delegate_schema_not_in_toolbox(self):
        names = [s["function"]["name"] for s in ToolBox.schemas()]
        self.assertNotIn("delegate_tasks", names,
                         "delegate_tasks 绝不能进 ToolBox.schemas()——"
                         "那是子代理的工具集来源")

    def test_delegate_schema_shape(self):
        fn = DELEGATE_SCHEMA["function"]
        self.assertEqual(DELEGATE_SCHEMA["type"], "function")
        self.assertIn("tasks", fn["parameters"]["properties"])
        self.assertEqual(fn["parameters"]["required"], ["tasks"])

    def test_main_agent_gets_delegate_tool(self):
        ag = InsightAgent(_FakeDs(), llm=_FakeLlm(),
                          team=AgentTeam(_FakeDs(), llm=_FakeLlm(), llm_factory=_FakeLlm))
        # 跑一次，主代理会调 chat_tools —— 检查它拿到的工具集
        ag.run("测试问题")
        names = ag.llm.calls_made[0]["tool_names"]
        self.assertIn("delegate_tasks", names)

    def test_agent_without_team_has_no_delegate_tool(self):
        ag = InsightAgent(_FakeDs(), llm=_FakeLlm())
        ag.run("测试问题")
        names = ag.llm.calls_made[0]["tool_names"]
        self.assertNotIn("delegate_tasks", names,
                         "没有编排团队时不该给这个工具，模型会喊空工具")


# ------------------------------------------------------------------ 并行

class TestParallelism(unittest.TestCase):
    def test_children_run_concurrently(self):
        """并行是真的并行：总耗时应接近单个子代理，而非累加。"""
        team = AgentTeam(_FakeDs(), llm=_FakeLlm(),
                         llm_factory=lambda: _FakeLlm(delay=0.4))
        t0 = time.time()
        team.delegate(["a", "b", "c"], depth=0)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0,
                        f"3 个 0.4s 的子代理串行要 1.2s，实测 {elapsed:.2f}s")


# ------------------------------------------------------------------ 数字审计

class TestHumanizeDelegateGoals(unittest.TestCase):
    """子任务目标里不能带 wxid 机器标识。

    端到端实测踩到：主代理查完 find_contact 后把 sessionId 抄进子任务目标，
    结果子代理看到的是 `wxid_dbkamfm4sr8122`，前端分支卡片也把机器标识
    显示给用户——违反「不给人看机器标识」的既有纪律。
    """

    class _Ds:
        def find_session(self, q):
            if q == "wxid_abc123":
                return {"sessionId": "wxid_abc123", "displayName": "王小红"}
            return None

    def test_replaces_wxid_with_name(self):
        from core.agent_engine import _humanize_text
        out = _humanize_text(self._Ds(), "分析会话 wxid_abc123 的聊天")
        self.assertIn("王小红", out)
        self.assertNotIn("wxid_abc123", out)

    def test_unknown_id_left_alone(self):
        from core.agent_engine import _humanize_text
        out = _humanize_text(self._Ds(), "分析会话 wxid_zzz999 的聊天")
        self.assertIn("wxid_zzz999", out, "查不到就不能瞎替换")

    def test_plain_text_unchanged(self):
        from core.agent_engine import _humanize_text
        self.assertEqual(_humanize_text(self._Ds(), "查一下王小红"), "查一下王小红")


# ------------------------------------------------------------------ 数字审计

class TestStatsSessionScope(unittest.TestCase):
    """统计口径：传了 sessionId 就必须按单会话算。

    端到端实测挖出的两个真 bug：
      1. `time_distribution` 漏传 session → 「某人的时段分布」返回全库分布，
         模型拿 224025 条全库数据当这个人的分布当结论；
      2. `types` 同样漏传 → 子代理自己发现了「和各自会话的总数对不上」。

    这类 bug 单测抓不到（参数确实传了，只是没传下去），
    必须用 mock 记录 Analytics 收到的实参来锁。
    """

    class _FakeAnalytics:
        """记录 time_stats / session_stats 收到的实参。"""

        last_time_args = None
        last_session_args = None

        def __init__(self, ds):
            pass

        def time_stats(self, by="month", session=None, from_ts=None, to_ts=None):
            TestStatsSessionScope._FakeAnalytics.last_time_args = {
                "by": by, "session": session}
            return {"by": by, "distribution": {"01": 10}, "total": 10}

        def session_stats(self, session, from_ts=None, to_ts=None):
            TestStatsSessionScope._FakeAnalytics.last_session_args = session
            return {"session": {"displayName": "王小红"},
                    "byType": {"text": 8, "image": 2}}

        def global_stats(self, from_ts=None, to_ts=None):
            return {"totalMessages": 999999, "byType": {"text": 999999}}

    class _Ds:
        def sessions(self):
            return []

        def find_session(self, q):
            return None

    def setUp(self):
        # Analytics 在 t_chat_stats 里是**函数内延迟导入**的
        # （from core.analytics import Analytics），
        # 所以要打到 core.analytics 模块上，不能打 core.agent_tools。
        import core.analytics as an
        self._orig = an.Analytics
        an.Analytics = self._FakeAnalytics
        self.box = ToolBox(self._Ds())
        TestStatsSessionScope._FakeAnalytics.last_time_args = None
        TestStatsSessionScope._FakeAnalytics.last_session_args = None

    def tearDown(self):
        import core.analytics as an
        an.Analytics = self._orig

    def test_time_distribution_passes_session(self):
        self.box.run("chat_stats", {"metric": "time_distribution",
                                    "sessionId": "wxid_abc"})
        args = self._FakeAnalytics.last_time_args
        self.assertIsNotNone(args, "time_stats 根本没被调用")
        self.assertEqual(args["session"], "wxid_abc",
                         "session 没传下去 → 返回的是全库分布")

    def test_time_distribution_without_session_is_global(self):
        self.box.run("chat_stats", {"metric": "time_distribution"})
        args = self._FakeAnalytics.last_time_args
        self.assertIsNone(args["session"], "没传 sessionId 时应是全库口径")

    def test_types_uses_session_stats_when_session_given(self):
        r = self.box.run("chat_stats", {"metric": "types",
                                        "sessionId": "wxid_abc"})
        self.assertEqual(self._FakeAnalytics.last_session_args, "wxid_abc",
                         "types 带 sessionId 时必须走 session_stats")
        self.assertIn("王小红", r["summary"], "摘要要说明是哪个会话")
        self.assertEqual(r["data"]["byType"]["text"], 8,
                         "返回的必须是单会话数据，不是全库的 999999")

    def test_types_without_session_is_global(self):
        r = self.box.run("chat_stats", {"metric": "types"})
        self.assertIsNone(self._FakeAnalytics.last_session_args)
        self.assertEqual(r["data"]["byType"]["text"], 999999)


# ------------------------------------------------------------------ 数字审计

class TestNumberAuditAcrossBranches(unittest.TestCase):
    """子代理给的数字必须能被父代理的数字审计认可。"""

    def test_child_numbers_enter_parent_audit_scope(self):
        ds = _FakeDs()

        class NumberLlm(_FakeLlm):
            pass

        team = AgentTeam(ds, llm=_FakeLlm(), llm_factory=_FakeLlm)
        br = team.run_branch("x", depth=1)
        br.numbers = {"2814"}
        # 手工构造：父代理引用子代理的数字后，审计不应报「没出处」
        from core.agent_engine import _audit_numbers, AgentRun
        run = AgentRun("q")
        run.tool_numbers |= br.numbers
        run.answer = "一共 2814 条消息。"
        out = _audit_numbers(run)
        self.assertNotIn("核对提醒", out,
                         "子代理的数字进了审计范围后不应被误报")


if __name__ == "__main__":
    unittest.main()
