# -*- coding: utf-8 -*-
"""迭代 8 的**接线**测试（静态断言 + 少量行为断言）。

为什么单独一个文件：迭代 7 踩过的坑——
「mock 直接注入的渲染测试抓不到『后端没把 contract 传给前端』」。
所以这里不只测模块对不对，还测**线有没有接上**：

  1. llmconfig 的阶段模型读写被 server 真正调用
  2. agent_engine 的成文阶段真的接在主循环上
  3. modelRouting 从后端一路传到前端模板
  4. 前端模板里的函数都在 setup 里 return（Vue 静默失败的经典坑）
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestLlmConfigWiring(unittest.TestCase):
    def test_server_has_stage_models_api(self):
        src = _read("gui/server.py")
        self.assertIn("def api_stage_models()", src)
        self.assertIn("def api_stage_models_set(", src)

    def test_stage_models_route_registered(self):
        src = _read("gui/server.py")
        self.assertIn('"/api/stage-models"', src)

    def test_post_route_dispatches_to_set(self):
        src = _read("gui/server.py")
        self.assertIn("api_stage_models_set", src)

    def test_save_stage_models_does_not_touch_other_keys(self):
        """保存阶段模型不得动 endpoints / activeEndpoint。"""
        src = _read("core/llmconfig.py")
        body = src[src.index("def save_stage_models("):src.index("def resolve_stage_model(")]
        self.assertNotIn('cfg["endpoints"]', body)
        self.assertNotIn('cfg["activeEndpoint"]', body)
        self.assertNotIn('cfg["baseUrl"]', body)


class TestAgentEngineWiring(unittest.TestCase):
    def test_resolve_models_exists(self):
        src = _read("core/agent_engine.py")
        self.assertIn("def _resolve_models(", src)

    def test_collect_client_used_in_main_loop(self):
        """主循环必须用取证客户端，不能还用 self.llm 直连。"""
        src = _read("core/agent_engine.py")
        body = src[src.index("def run(self, question"):src.index("def _compose(")]
        self.assertIn("self._collect_client().chat_tools(", body)
        # 主循环里不该再有裸的 self.llm.chat_tools
        self.assertNotIn("self.llm.chat_tools(", body)

    def test_compose_runs_only_when_distinct(self):
        src = _read("core/agent_engine.py")
        body = src[src.index("def run(self, question"):src.index("def _compose(")]
        self.assertIn('if self._model_routing.get("distinct"):', body)

    def test_compose_replaces_answer(self):
        src = _read("core/agent_engine.py")
        body = src[src.index("def run(self, question"):src.index("def _compose(")]
        self.assertIn("run.answer = composed", body)

    def test_compose_applied_flag_set(self):
        src = _read("core/agent_engine.py")
        body = src[src.index("def run(self, question"):src.index("def _compose(")]
        self.assertIn('run.model_routing["applied"] = True', body)

    def test_same_model_records_compose_error(self):
        """配了成文模型但与取证同名 → 必须记明原因，不能静默跳过。"""
        src = _read("core/agent_engine.py")
        body = src[src.index("def run(self, question"):src.index("def _compose(")]
        self.assertIn("不构成交叉核验", body)

    def test_model_routing_passed_to_contract(self):
        """契约判定必须拿到 modelRouting（否则 compose 阶段永远 SKIPPED）。"""
        src = _read("core/agent_engine.py")
        body = src[src.index("def run(self, question"):src.index("def _compose(")]
        self.assertIn('"modelRouting": run.model_routing', body)

    def test_evidence_pack_collected_from_tool_results(self):
        """证据包来自工具返回（不是取证模型的自述）。"""
        src = _read("core/agent_engine.py")
        self.assertIn("evidence_pack.append({", src)
        self.assertIn('"data": res.get("data") or {}', src)

    def test_compose_failure_returns_empty_not_raise(self):
        """成文失败返回空串（调用方据此保留原答案），不能抛。"""
        src = _read("core/agent_engine.py")
        body = src[src.index("def _compose("):src.index("def _run_delegate(")]
        self.assertIn("except Exception:", body)
        self.assertIn("return \"\"", body)

    def test_audit_still_runs_after_compose(self):
        """数字审计必须在成文之后跑（审计的是最终答案）。"""
        src = _read("core/agent_engine.py")
        i_compose = src.index("run.answer = composed")
        i_audit = src.index("run.answer = _audit_numbers(run)")
        self.assertLess(i_compose, i_audit)

    def test_contract_before_audit_still_holds(self):
        """迭代 7 的顺序铁律不能被迭代 8 破坏。"""
        src = _read("core/agent_engine.py")
        i_eval = src.index("pipeline.evaluate(")
        i_audit = src.index("run.answer = _audit_numbers(run)")
        self.assertLess(i_eval, i_audit)


class TestPipelineWiring(unittest.TestCase):
    def test_pipeline_has_cross_model_section(self):
        src = _read("core/pipeline.py")
        self.assertIn('"crossModel": cross_model', src)

    def test_pipeline_has_compose_stage(self):
        src = _read("core/pipeline.py")
        self.assertIn('"id": "compose"', src)

    def test_pipeline_has_no_self_review_rule(self):
        src = _read("core/pipeline.py")
        self.assertIn('"id": "no_self_review"', src)

    def test_pipeline_has_predicate_definition(self):
        src = _read("core/pipeline.py")
        self.assertIn('"id": "distinct_configured_model"', src)

    def test_pipeline_has_compose_failed_status(self):
        src = _read("core/pipeline.py")
        self.assertIn('"COMPOSE_FAILED"', src)

    def test_compose_failed_is_not_blocking(self):
        """成文失败不阻断（答案还是有的）。"""
        src = _read("core/pipeline.py")
        body = src[src.index('"COMPOSE_FAILED": {'):src.index("}\n\n\n# ---")]
        self.assertIn('"ok_to_proceed": True', body)


class TestServerResultWiring(unittest.TestCase):
    def test_insight_result_carries_model_routing(self):
        src = _read("gui/server.py")
        self.assertIn('out["modelRouting"] = run_obj.model_routing or {}', src)

    def test_run_persists_model_routing(self):
        src = _read("gui/server.py")
        self.assertIn('"modelRouting": out.get("modelRouting", {})', src)


class TestFrontendWiring(unittest.TestCase):
    def test_insight_view_renders_cross_model(self):
        src = _read("gui/assets/v2/views-insight.js")
        self.assertIn("v2-ia-crossmodel", src)
        self.assertIn("t.contract.crossModel.collectModel", src)
        self.assertIn("t.contract.crossModel.writeModel", src)

    def test_insight_view_has_predicate_anchor(self):
        """语义锚点：Playwright 端到端靠它定位交叉核验块。"""
        src = _read("gui/assets/v2/views-insight.js")
        self.assertIn(':data-predicate="t.contract.crossModel.predicate"', src)
        self.assertIn(':data-predicate-satisfied=', src)

    def test_settings_view_has_stage_model_panel(self):
        src = _read("gui/assets/v2/views-settings.js")
        self.assertIn("v2-sm-card", src)
        self.assertIn(":data-stage-role=", src)

    def test_settings_view_stage_select_anchor(self):
        src = _read("gui/assets/v2/views-settings.js")
        self.assertIn(":data-stage-select=", src)

    def test_settings_view_warns_same_model(self):
        """配成同一个模型要有明确提示（这是这条机制最容易白配的地方）。"""
        src = _read("gui/assets/v2/views-settings.js")
        self.assertIn("stageSame", src)
        self.assertIn("不构成交叉核验", src)

    def test_settings_stage_functions_returned(self):
        """Vue 模板里调的函数必须在 setup 里 return，否则 not a function。"""
        src = _read("gui/assets/v2/views-settings.js")
        ret = src[src.rindex("return {"):src.rindex("};")]
        for fn in ("loadStage", "saveStage", "stageRoles", "stageDraft",
                   "stageMsg", "stageOk", "stageBusy", "stageSame"):
            self.assertIn(fn, ret, f"{fn} 没在 setup 的 return 里")

    def test_settings_calls_stage_api(self):
        src = _read("gui/assets/v2/views-settings.js")
        self.assertIn('M.api.get("/api/stage-models")', src)
        self.assertIn('M.api.post("/api/stage-models"', src)


class TestCssWiring(unittest.TestCase):
    def test_cross_model_styles_exist(self):
        css = _read("gui/assets/v2/app.css")
        self.assertIn(".v2-ia-crossmodel", css)
        self.assertIn(".v2-ia-crossmodel.on", css)

    def test_stage_model_card_styles_exist(self):
        css = _read("gui/assets/v2/app.css")
        for cls in (".v2-sm-row", ".v2-sm-card", ".v2-sm-select"):
            self.assertIn(cls, css)

    def test_cross_model_uses_theme_tokens(self):
        """样式必须走 theme.css 令牌（不能硬编码颜色）。"""
        css = _read("gui/assets/v2/app.css")
        body = css[css.index(".v2-ia-crossmodel {"):css.index(".v2-ia-crossmodel.on")]
        self.assertIn("var(--", body)
        for hard in ("#fff", "#000", "rgb("):
            self.assertNotIn(hard, body)


if __name__ == "__main__":
    unittest.main()
