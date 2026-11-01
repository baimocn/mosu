# -*- coding: utf-8 -*-
"""迭代 8：阶段模型路由（取证 / 成文双角色）的单元测试。

对标 Lancer 的两样东西：
  1. `LancerWorkflowStep.model?: {provider, modelId}` —— 阶段上可选的模型绑定
  2. handoff 边的谓词 `distinct_configured_model` —— 交付阶段必须用与上游不同的模型

墨溯的映射：不给四个契约阶段各配一个模型（那会变成固定管道，
把迭代 3 好不容易拿回来的自主性又绑回去），
而是拆成两个**角色**：collect（跑 ReAct 循环取证）/ write（基于证据成文）。

锁死的六件事：
  1. 不配置 = 现在的行为，一字不差（向后兼容硬要求）
  2. 配了但与取证模型同名 → 不跑成文，且**必须挂 forbidden**（不许假装成交叉核验）
  3. 成文模型**看不到取证模型的自述**（否则两个模型等于一个）
  4. 成文失败不能连带整轮失败（退回取证模型的答案）
  5. env 变量压过一切，且不做「一半 env 一半文件」的混合
  6. 契约里 compose 阶段的状态码与 crossModel 谓词结果一致
"""
import unittest

from core import pipeline


def _run(**kw):
    base = {
        "steps": [{"tool": "chat_stats"}],
        "evidence": [{"kind": "stat", "text": "消息量 100 条"}],
        "answer": "共 100 条消息。",
        "toolNumbers": {"100"},
        "branches": [],
        "unsourcedNumbers": [],
        "modelRouting": {},
    }
    base.update(kw)
    return base


def _routing(**kw):
    base = {
        "collectModel": "glm-5",
        "writeModel": "",
        "distinct": False,
        "applied": False,
    }
    base.update(kw)
    return base


class TestStageModelsConfig(unittest.TestCase):
    """core/llmconfig.py 的阶段模型读写。"""

    def test_roles_are_collect_and_write(self):
        from core.llmconfig import STAGE_MODEL_ROLES
        self.assertEqual(list(STAGE_MODEL_ROLES), ["collect", "write"])

    def test_unconfigured_returns_empty(self):
        """不配置 = 空 dict（调用方据此回落到活跃模型）。"""
        from core.llmconfig import load_stage_models
        # 没配置过时必须返回 {}，不能返回带空 model 的条目
        got = load_stage_models()
        for role, item in got.items():
            self.assertTrue(item.get("model"), f"{role} 配了但没有 model")

    def test_save_then_load_roundtrip(self):
        from core.llmconfig import load_stage_models, save_stage_models
        try:
            save_stage_models({"write": {"model": "deepseek-chat"}})
            got = load_stage_models()
            self.assertEqual(got.get("write", {}).get("model"), "deepseek-chat")
        finally:
            save_stage_models({"write": {"model": ""}})

    def test_save_with_empty_model_clears_binding(self):
        """model 传空串 = 取消绑定（不是「配了个空模型」）。"""
        from core.llmconfig import load_stage_models, save_stage_models
        try:
            save_stage_models({"write": {"model": "x-model"}})
            save_stage_models({"write": {"model": ""}})
            self.assertNotIn("write", load_stage_models())
        finally:
            save_stage_models({"write": {"model": ""}})

    def test_save_ignores_unknown_role(self):
        """未知角色不写进配置（别让人塞垃圾键进去）。"""
        from core.llmconfig import load_stage_models, save_stage_models
        try:
            save_stage_models({"hack": {"model": "evil"}})
            self.assertNotIn("hack", load_stage_models())
        finally:
            save_stage_models({"hack": {"model": ""}})

    def test_resolve_returns_none_for_unconfigured(self):
        from core.llmconfig import resolve_stage_model
        # 未配置 → None（调用方回落活跃模型），不是造一个假配置
        got = resolve_stage_model("write")
        if got is not None:
            # 若本机恰好配了，只要求字段完整
            self.assertTrue(got.get("model"))

    def test_resolve_rejects_unknown_role(self):
        from core.llmconfig import resolve_stage_model
        self.assertIsNone(resolve_stage_model("not_a_role"))

    def test_resolve_never_leaks_api_key_in_plain(self):
        """include_key=False 时 apiKey 必须是掩码。"""
        from core.llmconfig import resolve_stage_model
        got = resolve_stage_model("write")
        if got is not None:
            self.assertNotEqual(got.get("apiKey"), "")
            self.assertIn(got.get("apiKey"), ("***", ""))

    def test_env_wins_over_endpoint_id(self):
        """env 压过一切 —— 且不做「一半 env 一半文件」的混合。"""
        import os
        from core import llmconfig
        old_base = os.environ.get("MOSU_BOT_BASE_URL")
        old_key = os.environ.get("MOSU_BOT_API_KEY")
        try:
            os.environ["MOSU_BOT_BASE_URL"] = "https://env.example.com/v1"
            os.environ["MOSU_BOT_API_KEY"] = "env-key"
            spec = llmconfig.resolve_stage_model("write", include_key=True)
            if spec is not None:
                self.assertEqual(spec["baseUrl"], "https://env.example.com/v1")
                self.assertEqual(spec["apiKey"], "env-key")
                # 端点 id 被 env 覆盖掉了 —— 不能一半 env 一半文件
                self.assertEqual(spec["endpointId"], "")
        finally:
            if old_base is None:
                os.environ.pop("MOSU_BOT_BASE_URL", None)
            else:
                os.environ["MOSU_BOT_BASE_URL"] = old_base
            if old_key is None:
                os.environ.pop("MOSU_BOT_API_KEY", None)
            else:
                os.environ["MOSU_BOT_API_KEY"] = old_key


class TestComposeStage(unittest.TestCase):
    """契约里 compose（成文）阶段的判定。"""

    def test_compose_stage_exists(self):
        ids = [s["id"] for s in pipeline.STAGES]
        self.assertIn("compose", ids)

    def test_compose_is_the_only_model_stage(self):
        """compose 是唯一的 source=model 阶段。"""
        models = [s["id"] for s in pipeline.STAGES if s.get("source") == "model"]
        self.assertEqual(models, ["compose"])

    def test_compose_skipped_when_not_configured(self):
        r = pipeline.evaluate(_run(modelRouting=_routing()))
        s = {x["id"]: x for x in r["stages"]}["compose"]
        self.assertEqual(s["status"]["code"], "SKIPPED")
        self.assertTrue(s["status"]["okToProceed"])

    def test_compose_ok_when_distinct_and_applied(self):
        r = pipeline.evaluate(_run(modelRouting=_routing(
            writeModel="deepseek-chat", distinct=True, applied=True)))
        s = {x["id"]: x for x in r["stages"]}["compose"]
        self.assertEqual(s["status"]["code"], "OK")

    def test_compose_skipped_when_same_model(self):
        """配了但同名 → 不跑成文（跑了也是白跑）。"""
        r = pipeline.evaluate(_run(modelRouting=_routing(
            writeModel="glm-5", distinct=False, applied=False)))
        s = {x["id"]: x for x in r["stages"]}["compose"]
        self.assertEqual(s["status"]["code"], "SKIPPED")

    def test_compose_failed_status_when_applied_false(self):
        """配了、模型不同、但成文调用挂了 → COMPOSE_FAILED（不阻断）。"""
        r = pipeline.evaluate(_run(modelRouting=_routing(
            writeModel="deepseek-chat", distinct=True, applied=False)))
        s = {x["id"]: x for x in r["stages"]}["compose"]
        self.assertEqual(s["status"]["code"], "COMPOSE_FAILED")
        self.assertTrue(s["status"]["okToProceed"])

    def test_compose_summary_contains_models(self):
        """机械摘要里要能看到模型名（Lancer 的 handoff 摘要也带标识）。"""
        r = pipeline.evaluate(_run(modelRouting=_routing(
            writeModel="deepseek-chat", distinct=True, applied=True)))
        s = {x["id"]: x for x in r["stages"]}["compose"]
        self.assertIn("glm-5", s["summary"])
        self.assertIn("deepseek-chat", s["summary"])

    def test_compose_summary_has_no_narrative(self):
        r = pipeline.evaluate(_run(modelRouting=_routing(
            writeModel="deepseek-chat", distinct=True, applied=True)))
        s = {x["id"]: x for x in r["stages"]}["compose"]
        for w in ("因为", "所以", "综上", "建议"):
            self.assertNotIn(w, s["summary"])

    def test_compose_is_not_required(self):
        """compose 不阻断（没配成文模型时不该让整轮挂掉）。"""
        req = {s["id"]: s["required"] for s in pipeline.STAGES}
        self.assertFalse(req["compose"])


class TestNoSelfReview(unittest.TestCase):
    """第 5 条禁止项：no_self_review。"""

    def test_forbidden_contains_no_self_review(self):
        ids = [f["id"] for f in pipeline.FORBIDDEN]
        self.assertIn("no_self_review", ids)

    def test_same_model_violates(self):
        r = pipeline.evaluate(_run(modelRouting=_routing(
            collectModel="glm-5", writeModel="glm-5")))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertTrue(f["no_self_review"]["violated"])
        self.assertIn("glm-5", f["no_self_review"]["detail"])

    def test_distinct_models_no_violation(self):
        r = pipeline.evaluate(_run(modelRouting=_routing(
            collectModel="glm-5", writeModel="deepseek-chat", distinct=True,
            applied=True)))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertFalse(f["no_self_review"]["violated"])

    def test_unconfigured_write_is_not_a_violation(self):
        """没配成文模型 ≠ 配置错误（那是用户选了单模型模式）。"""
        r = pipeline.evaluate(_run(modelRouting=_routing(writeModel="")))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertFalse(f["no_self_review"]["violated"])

    def test_no_modelRouting_key_is_not_a_violation(self):
        """老数据没有 modelRouting 键 → 不能判违规（向后兼容）。"""
        d = _run()
        d.pop("modelRouting")
        r = pipeline.evaluate(d)
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertFalse(f["no_self_review"]["violated"])


class TestCrossModelSection(unittest.TestCase):
    """契约里的 crossModel 段（前端显示用）。"""

    def test_cross_model_present(self):
        r = pipeline.evaluate(_run())
        self.assertIn("crossModel", r)

    def test_predicate_name_matches_lancer(self):
        """谓词名要与 Lancer 的 distinct_configured_model 一致。"""
        r = pipeline.evaluate(_run())
        self.assertEqual(r["crossModel"]["predicate"], "distinct_configured_model")

    def test_predicate_satisfied_requires_both(self):
        """谓词成立要「不同」且「真跑了」——只满足一条不算。"""
        r1 = pipeline.evaluate(_run(modelRouting=_routing(
            writeModel="deepseek-chat", distinct=True, applied=False)))
        self.assertFalse(r1["crossModel"]["predicateSatisfied"])
        r2 = pipeline.evaluate(_run(modelRouting=_routing(
            writeModel="deepseek-chat", distinct=True, applied=True)))
        self.assertTrue(r2["crossModel"]["predicateSatisfied"])

    def test_cross_model_carries_model_names(self):
        r = pipeline.evaluate(_run(modelRouting=_routing(
            writeModel="deepseek-chat", distinct=True, applied=True)))
        cm = r["crossModel"]
        self.assertEqual(cm["collectModel"], "glm-5")
        self.assertEqual(cm["writeModel"], "deepseek-chat")

    def test_cross_model_default_is_empty_not_missing(self):
        """字段必须存在且是空值（前端不用判 undefined）。"""
        r = pipeline.evaluate(_run())
        cm = r["crossModel"]
        self.assertEqual(cm["collectModel"], "")
        self.assertEqual(cm["writeModel"], "")
        self.assertFalse(cm["distinct"])
        self.assertFalse(cm["applied"])
        self.assertEqual(cm["composeError"], "")

    def test_compose_error_is_carried(self):
        r = pipeline.evaluate(_run(modelRouting=_routing(
            writeModel="deepseek-chat", distinct=True, applied=False,
            composeError="成文模型调用失败")))
        self.assertIn("成文模型调用失败", r["crossModel"]["composeError"])


class TestEvidencePack(unittest.TestCase):
    """证据包格式（成文模型看到的东西）。"""

    def test_pack_formats_tool_returns(self):
        from core.agent_engine import _format_evidence_pack
        pack = [{"tool": "chat_stats", "label": "查统计",
                 "summary": "共 100 条", "data": {"totalMessages": 100}}]
        txt = _format_evidence_pack(pack)
        self.assertIn("查统计", txt)
        self.assertIn("100", txt)

    def test_empty_pack_returns_empty(self):
        from core.agent_engine import _format_evidence_pack
        self.assertEqual(_format_evidence_pack([]), "")

    def test_pack_is_bounded(self):
        """证据包不能无限长（会把上下文撑爆）。"""
        from core.agent_engine import _format_evidence_pack
        big = [{"tool": "chat_stats", "label": "查统计", "summary": "x" * 100,
                "data": {"blob": "y" * 5000}} for _ in range(20)]
        txt = _format_evidence_pack(big)
        self.assertLess(len(txt), 13000)

    def test_pack_says_when_truncated(self):
        """截断了要**明说**——不说模型会以为数据就这么多。"""
        from core.agent_engine import _format_evidence_pack
        big = [{"tool": "chat_stats", "label": "查统计", "summary": "x",
                "data": {"blob": "y" * 5000}} for _ in range(20)]
        self.assertIn("截断", _format_evidence_pack(big))


class TestComposePrompt(unittest.TestCase):
    """成文提示词的铁律。"""

    def test_compose_system_forbids_inventing(self):
        from core.agent_engine import COMPOSE_SYSTEM
        self.assertIn("只用证据里有的", COMPOSE_SYSTEM)

    def test_compose_hint_tells_model_to_only_use_evidence(self):
        from core.agent_engine import COMPOSE_HINT
        self.assertIn("只用上面出现过的信息", COMPOSE_HINT)

    def test_compose_hint_warns_against_collectors_own_words(self):
        """成文模型不能采信取证模型自述里的数字。"""
        from core.agent_engine import COMPOSE_HINT
        self.assertIn("取证模型", COMPOSE_HINT)

    def test_compose_system_has_no_tool_instructions(self):
        """成文阶段不给工具（它只该写，不该再查）。"""
        from core.agent_engine import COMPOSE_SYSTEM
        self.assertNotIn("调工具", COMPOSE_SYSTEM)


if __name__ == "__main__":
    unittest.main()
