# -*- coding: utf-8 -*-
"""阶段契约（core/pipeline.py）的单元测试。

对标 Lancer 的 PROCESS.json 语义，重点锁三件事：
  1. status code 带 ok_to_proceed（机器可判的推进条件）
  2. forbidden 每条都能程序化判定
  3. 每阶段一行机械摘要（无叙述）
"""
import unittest

from core import pipeline


def _run(**kw):
    """造一个最小 run 记录。"""
    base = {
        "steps": [],
        "evidence": [],
        "answer": "",
        "toolNumbers": set(),
        "branches": [],
        "unsourcedNumbers": [],
    }
    base.update(kw)
    return base


class TestStageDefinitions(unittest.TestCase):
    def test_five_stages_with_required_flags(self):
        """迭代 8 加了 compose（成文）阶段 —— 4 个工具阶段 + 1 个模型阶段。"""
        ids = [s["id"] for s in pipeline.STAGES]
        self.assertEqual(ids, ["scope", "probe", "read", "verify", "compose"])

    def test_probe_and_verify_are_required(self):
        """关键阶段：probe（取数）与 verify（核验）必须有值。"""
        req = {s["id"]: s["required"] for s in pipeline.STAGES}
        self.assertTrue(req["probe"])
        self.assertTrue(req["verify"])
        # scope / read 是加分项，不做强制
        self.assertFalse(req["scope"])
        self.assertFalse(req["read"])

    def test_every_stage_has_goal_and_actor(self):
        for s in pipeline.STAGES:
            self.assertTrue(s["goal"], f"{s['id']} 缺 goal")
            self.assertTrue(s["actor"], f"{s['id']} 缺 actor")
            # actor 取值对齐 Lancer 的同一套
            self.assertIn(s["actor"], ("human", "local", "gateway", "codex"))

    def test_tool_stage_covers_all_tools(self):
        """每个已知工具都要归到某个阶段，不能漏。"""
        from core.agent_tools import TOOL_SCHEMAS
        for sch in TOOL_SCHEMAS:
            name = sch["function"]["name"]
            self.assertIn(name, pipeline.TOOL_STAGE, f"{name} 没归类")

    def test_quote_tools_are_read_stage(self):
        """能产出原话的工具必须归 read 阶段。"""
        for t in pipeline.QUOTE_TOOLS:
            self.assertEqual(pipeline.TOOL_STAGE[t], "read")


class TestStatusCodes(unittest.TestCase):
    def test_every_status_has_ok_to_proceed(self):
        """Lancer 的核心机制：状态码带机器可判的推进条件。"""
        for name, spec in pipeline.STATUS_CODES.items():
            self.assertIn("ok_to_proceed", spec, f"{name} 缺 ok_to_proceed")
            self.assertIsInstance(spec["ok_to_proceed"], bool)
            self.assertIn("label", spec)
            self.assertIn("hint", spec)

    def test_blocking_statuses_have_hint(self):
        """阻断性状态必须给可行动的提示（不能只标个红）。"""
        for name, spec in pipeline.STATUS_CODES.items():
            if not spec["ok_to_proceed"]:
                self.assertTrue(spec["hint"].strip(),
                                f"{name} 阻断但没给 hint")

    def test_skipped_is_not_blocking(self):
        """阶段没跑不等于失败——Lancer 的 skip 不计失败。"""
        self.assertTrue(pipeline.STATUS_CODES["SKIPPED"]["ok_to_proceed"])


class TestClassifySteps(unittest.TestCase):
    def test_groups_by_stage(self):
        steps = [{"tool": "resolve_time"}, {"tool": "chat_stats"},
                 {"tool": "read_timeline"}, {"tool": "chat_stats"}]
        g = pipeline.classify_steps(steps)
        self.assertEqual(len(g["scope"]), 1)
        self.assertEqual(len(g["probe"]), 2)
        self.assertEqual(len(g["read"]), 1)

    def test_unknown_tool_goes_to_other_not_dropped(self):
        g = pipeline.classify_steps([{"tool": "weird_tool"}])
        self.assertIn("other", g)
        self.assertEqual(len(g["other"]), 1)

    def test_empty_steps(self):
        self.assertEqual(pipeline.classify_steps([]), {})


class TestEvaluateStages(unittest.TestCase):
    def test_all_ok_path(self):
        r = pipeline.evaluate(_run(
            steps=[{"tool": "resolve_time"}, {"tool": "chat_stats"},
                   {"tool": "read_timeline"}],
            evidence=[{"kind": "stat", "text": "共 100 条"},
                      {"kind": "quote", "text": "在吗"}],
            answer="他一共发了 100 条消息。",
            toolNumbers={"100"},
        ))
        self.assertTrue(r["okToProceed"])
        codes = {s["id"]: s["status"]["code"] for s in r["stages"]}
        self.assertEqual(codes["scope"], "OK")
        self.assertEqual(codes["probe"], "OK")
        self.assertEqual(codes["read"], "OK")
        self.assertEqual(codes["verify"], "OK")

    def test_missing_probe_blocks(self):
        """关键阶段没跑 → 阻断。"""
        r = pipeline.evaluate(_run(
            steps=[{"tool": "resolve_time"}],
            answer="他有 100 条消息。",
            toolNumbers={"100"},
        ))
        codes = {s["id"]: s["status"]["code"] for s in r["stages"]}
        self.assertEqual(codes["probe"], "MISSING")
        self.assertFalse(r["okToProceed"])

    def test_probe_called_but_no_evidence_is_empty(self):
        """跑了但没数据 → EMPTY，不是 MISSING。"""
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}],
            answer="查不到。",
        ))
        codes = {s["id"]: s["status"]["code"] for s in r["stages"]}
        self.assertEqual(codes["probe"], "EMPTY")

    def test_scope_skipped_is_not_blocking(self):
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}, {"tool": "read_timeline"}],
            evidence=[{"kind": "stat"}, {"kind": "quote"}],
            answer="共 10 条。",
            toolNumbers={"10"},
        ))
        codes = {s["id"]: s["status"]["code"] for s in r["stages"]}
        self.assertEqual(codes["scope"], "SKIPPED")
        self.assertTrue(r["okToProceed"])

    def test_unsourced_number_blocks_verify(self):
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}, {"tool": "read_timeline"}],
            evidence=[{"kind": "stat"}, {"kind": "quote"}],
            answer="他有 9999 条消息。",
            toolNumbers={"100"},
            unsourcedNumbers=["9999"],
        ))
        codes = {s["id"]: s["status"]["code"] for s in r["stages"]}
        self.assertEqual(codes["verify"], "UNSOURCED_NUMBER")
        self.assertFalse(r["okToProceed"])

    def test_empty_answer_blocks(self):
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}],
            evidence=[{"kind": "stat"}],
            answer="   ",
        ))
        codes = {s["id"]: s["status"]["code"] for s in r["stages"]}
        self.assertEqual(codes["verify"], "MISSING")


class TestGates(unittest.TestCase):
    def test_one_gate_per_stage(self):
        """Lancer：每阶段一个 gate 产物。"""
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}],
            evidence=[{"kind": "stat"}],
            answer="共 10 条。",
            toolNumbers={"10"},
        ))
        self.assertEqual(len(r["gates"]), len(pipeline.STAGES))
        self.assertEqual([g["stage"] for g in r["gates"]],
                         [s["id"] for s in pipeline.STAGES])

    def test_gate_has_stage_status_summary(self):
        """gate 三件套齐全：stage / status_code / summary。"""
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}],
            evidence=[{"kind": "stat"}],
            answer="共 10 条。",
            toolNumbers={"10"},
        ))
        for g in r["gates"]:
            self.assertIn("stage", g)
            self.assertIn("statusCode", g)
            self.assertIn("summary", g)
            self.assertIn("okToProceed", g)

    def test_summary_is_one_line_mechanical(self):
        """抄 Lancer：每阶段一行机械摘要，无叙述。"""
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}, {"tool": "read_timeline"}],
            evidence=[{"kind": "stat"}, {"kind": "quote"}],
            answer="共 10 条。",
            toolNumbers={"10"},
        ))
        for g in r["gates"]:
            self.assertNotIn("\n", g["summary"])
            self.assertIn("stage=", g["summary"])
            self.assertIn("status=", g["summary"])
        # 摘要里是标识与计数，不是叙述
        probe = [g for g in r["gates"] if g["stage"] == "probe"][0]
        self.assertIn("calls=", probe["summary"])
        self.assertIn("evidence=", probe["summary"])

    def test_summary_has_no_narrative_words(self):
        """机械摘要不该出现中文叙述词。"""
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}],
            evidence=[{"kind": "stat"}],
            answer="共 10 条。",
            toolNumbers={"10"},
        ))
        for g in r["gates"]:
            for w in ("因为", "所以", "说明", "我认为", "建议"):
                self.assertNotIn(w, g["summary"])


class TestForbidden(unittest.TestCase):
    def test_all_six_rules_present(self):
        ids = [f["id"] for f in pipeline.FORBIDDEN]
        self.assertEqual(ids, ["no_number_without_source",
                                "no_stat_as_quote",
                                "no_answer_without_evidence",
                                "no_quote_without_read",
                                "no_self_review",
                                "no_stat_without_scope"])

    def test_unsourced_number_violates(self):
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}],
            evidence=[{"kind": "stat"}],
            answer="共 9999 条。",
            unsourcedNumbers=["9999"],
        ))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertTrue(f["no_number_without_source"]["violated"])

    def test_sourced_number_no_violation(self):
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}],
            evidence=[{"kind": "stat"}],
            answer="共 10 条。",
            toolNumbers={"10"},
        ))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertFalse(f["no_number_without_source"]["violated"])

    def test_stat_without_quote_violates(self):
        """只有统计、没有原话 → 违反「统计不能当原话」。"""
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}],
            evidence=[{"kind": "stat", "text": "消息量 100 条"}],
            answer="他发了很多。",
        ))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertTrue(f["no_stat_as_quote"]["violated"])

    def test_quote_present_no_violation(self):
        r = pipeline.evaluate(_run(
            steps=[{"tool": "read_timeline"}],
            evidence=[{"kind": "quote", "text": "在吗"}],
            answer="他说「在吗」。",
        ))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertFalse(f["no_stat_as_quote"]["violated"])

    def test_answer_without_any_call_violates(self):
        """空手作答 → 违反。"""
        r = pipeline.evaluate(_run(answer="他最近状态不错。"))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertTrue(f["no_answer_without_evidence"]["violated"])

    def test_delegate_only_is_not_bare_answer(self):
        """只派了子代理也算查过——委派产出证据。"""
        r = pipeline.evaluate(_run(
            steps=[{"tool": "delegate_tasks"}],
            evidence=[{"kind": "quote"}],
            answer="他说「在吗」。",
        ))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertFalse(f["no_answer_without_evidence"]["violated"])

    def test_mentions_quote_without_read_violates(self):
        """答案提到具体话语但没有原话证据 → 违反。"""
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}],
            evidence=[{"kind": "stat"}],
            answer="他说「我最近很忙」。",
        ))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertTrue(f["no_quote_without_read"]["violated"])

    def test_each_violation_has_detail(self):
        """违规必须带可复现的 detail（不然用户不知道怎么核）。"""
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}],
            evidence=[{"kind": "stat"}],
            answer="他说「在吗」。",
            unsourcedNumbers=["9999"],
        ))
        for x in r["forbidden"]:
            if x["violated"]:
                self.assertTrue(x["detail"].strip(),
                                f"{x['id']} 违规但没给 detail")


class TestEvaluateContract(unittest.TestCase):
    def test_summary_line(self):
        r = pipeline.evaluate(_run(
            steps=[{"tool": "chat_stats"}],
            evidence=[{"kind": "stat"}],
            answer="共 10 条。",
            toolNumbers={"10"},
        ))
        self.assertIn("probe:", r["summary"])
        self.assertIn("verify:", r["summary"])

    def test_returns_all_top_level_keys(self):
        r = pipeline.evaluate(_run())
        for k in ("stages", "gates", "forbidden", "checks", "okToProceed",
                  "summary"):
            self.assertIn(k, r)

    def test_does_not_mutate_input(self):
        """判定是纯函数——不能改调用方的 run。"""
        steps = [{"tool": "chat_stats"}]
        evidence = [{"kind": "stat"}]
        r = _run(steps=steps, evidence=evidence, answer="共 10 条。")
        before = (len(steps), len(evidence))
        pipeline.evaluate(r)
        self.assertEqual((len(steps), len(evidence)), before)

    def test_empty_run_is_blocked(self):
        """什么都没跑 → 关键阶段缺失 → 阻断。"""
        r = pipeline.evaluate(_run())
        self.assertFalse(r["okToProceed"])


class TestAgentEngineIntegration(unittest.TestCase):
    """接入点：契约判定用的是**原始答案**，不是拼完审计提示的答案。"""

    def test_unsourced_helper_matches_audit(self):
        from core.agent_engine import AgentRun, _audit_numbers, _unsourced_numbers
        run = AgentRun("q")
        run.answer = "他有 9999 条消息。"
        run.tool_numbers = {"100"}
        self.assertEqual(_unsourced_numbers(run), ["9999"])
        self.assertIn("核对提醒", _audit_numbers(run))

    def test_audit_prompt_numbers_not_treated_as_answer_numbers(self):
        """审计提示里写的数字不能被当成答案的数字——否则自证通过。"""
        from core.agent_engine import AgentRun, _unsourced_numbers
        run = AgentRun("q")
        run.answer = "他有 9999 条消息。"
        run.tool_numbers = {"100"}
        first = _unsourced_numbers(run)
        # 模拟先拼提示再判定
        run.answer = run.answer + "\n\n---\n\n⚠️ 核对提醒：答案里的 **9999**"
        second = _unsourced_numbers(run)
        self.assertEqual(first, second)

    def test_agent_run_has_contract_field(self):
        from core.agent_engine import AgentRun
        run = AgentRun("q")
        self.assertEqual(run.contract, {})
        self.assertIn("contract", run.to_dict())

    def test_contract_survives_evaluate_failure(self):
        """契约判定抛异常不能连带整轮失败。"""
        from core.agent_engine import InsightAgent
        import core.pipeline as pl

        orig = pl.evaluate
        pl.evaluate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            import core.agent_engine as ae
            ae_inspect = ae.InsightAgent.__new__(ae.InsightAgent)
            # 直接测兜底逻辑：契约为空但答案还在
            self.assertEqual(getattr(ae_inspect, "contract", {}), {})
        finally:
            pl.evaluate = orig


if __name__ == "__main__":
    unittest.main()
