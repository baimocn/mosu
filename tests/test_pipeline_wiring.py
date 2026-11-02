# -*- coding: utf-8 -*-
"""阶段契约的静态接线检查。

为什么要有这组测试
------------------
渲染测试证明「给了 contract 数据时渲染正确」，
但证明不了「后端真的会传 contract 给前端」。
中间那几环（agent_engine 算契约 → server 传给前端 → 前端接住 → 落盘）
任何一环断了，界面都一片空白，而**所有单测照样全绿**。

这组测试就是把那几环逐个钉死。
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestPipelineModuleWiring(unittest.TestCase):
    def test_pipeline_module_exists(self):
        self.assertTrue((ROOT / "core" / "pipeline.py").exists())

    def test_pipeline_is_pure_stdlib(self):
        """零依赖铁律：只用标准库。"""
        src = _read("core/pipeline.py")
        imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", src, re.M)
        for mod in imports:
            root = mod.split(".")[0]
            self.assertIn(root, ("re", "typing", "__future__", "json",
                                 "dataclasses", "collections"),
                          f"core/pipeline.py 引入了非标准库 {mod}")

    def test_pipeline_does_not_import_dataset(self):
        """契约判定不能碰数据库——它只判定已发生的事实。"""
        src = _read("core/pipeline.py")
        self.assertNotIn("core.dataset", src)
        self.assertNotIn("from core import dataset", src)

    def test_evaluate_is_side_effect_free(self):
        """evaluate 不得修改传入的 run（纯判定）。"""
        src = _read("core/pipeline.py")
        # evaluate 里不应出现赋值给入参的写法
        body = src[src.index("def evaluate("):src.index("def _check_forbidden(")]
        self.assertNotIn("run[", body.replace('run.get("', '@@'),
                         "evaluate 里出现了对 run 的写入")
        self.assertNotIn("run.steps =", body)
        self.assertNotIn("run.answer =", body)


class TestAgentEngineWiring(unittest.TestCase):
    def test_agent_run_has_contract_field(self):
        src = _read("core/agent_engine.py")
        self.assertIn("self.contract", src)

    def test_to_dict_includes_contract(self):
        src = _read("core/agent_engine.py")
        m = re.search(r"def to_dict\(self\).*?return \{(.*?)\n        \}", src, re.S)
        self.assertIsNotNone(m)
        self.assertIn('"contract"', m.group(1))

    def test_run_calls_pipeline_evaluate(self):
        src = _read("core/agent_engine.py")
        self.assertIn("from core import pipeline", src)
        self.assertIn("pipeline.evaluate(", src)

    def test_contract_evaluated_before_audit_note(self):
        """顺序铁律：先判契约，再拼审计提示。

        反了的话，契约会拿「提示文本里的数字」当答案的数字来自证，
        永远判成「有出处」——自己给自己签通过。

        迭代 13：判定逻辑搬进了模块级 _evaluate_contract()（为了让端到端
        能真打坏它再验兜底），所以「调 evaluate 的位置」和「拼审计的位置」
        不再在同一个函数里。**不能比源文本的下标**——
        那样断言的是「代码长什么样」，不是「顺序对不对」。
        真正要锁的是**运行顺序**：契约判定发生在审计提示之前。
        所以这里找的是两处**调用点**在 run() 里的相对位置。
        """
        src = _read("core/agent_engine.py")
        run_body = src[src.index("def run(self, question"):src.index("def _compose(")]
        i_eval = run_body.index("run.contract = _evaluate_contract(run)")
        i_audit = run_body.index("run.answer = _audit_numbers(run)")
        self.assertLess(i_eval, i_audit,
                        "契约判定必须在审计提示之前（否则会自证）")

    def test_unsourced_helper_split_out(self):
        """无出处数字的判定要能单独取用（契约要用它）。"""
        src = _read("core/agent_engine.py")
        self.assertIn("def _unsourced_numbers(", src)
        self.assertIn("_unsourced_numbers(run)", src)

    def test_contract_failure_does_not_break_run(self):
        """契约判定抛异常不能连带整轮失败，且**不能静默**。

        迭代 13 改了两处：
        1. 判定逻辑搬进模块级 _evaluate_contract()（端到端要能真打坏它）
        2. 兜底不再用空 dict `{}`——空契约跟「这轮没做契约判定」在界面上
           长得一模一样，用户会以为答案经过校验。改成带 error 的结构。
        所以断言也跟着改：查的是**兜底结构带 error**，不是查字面量 `{}`。
        """
        src = _read("core/agent_engine.py")
        idx = src.index("def _evaluate_contract(")
        block = src[idx:idx + 1600]
        self.assertIn("except Exception", block)
        # 兜底必须带 error（可诊断）+ stages 为空（前端不画空壳）
        self.assertIn('"error"', block)
        self.assertIn('"stages": []', block)
        # 且不能是「run.contract = {}」这种静默兜底
        self.assertNotIn("run.contract = {}", block)

    def test_audit_numbers_still_appends_note(self):
        """既有行为不能丢：审计提示仍要拼在答案后面。"""
        src = _read("core/agent_engine.py")
        self.assertIn("核对提醒", src)
        self.assertIn("run.answer = _audit_numbers(run)", src)


class TestServerWiring(unittest.TestCase):
    def test_server_passes_contract_to_frontend(self):
        src = _read("gui/server.py")
        self.assertIn('out["contract"]', src)

    def test_server_persists_contract(self):
        """落盘也要带契约——回看历史时能看到当时哪些环节没过。"""
        src = _read("gui/server.py")
        self.assertIn('"contract": out.get("contract", {})', src)

    def test_contract_from_agent_run_not_reevaluated(self):
        """server 直接用 agent 算好的契约，不要自己再算一遍
        （两处算可能口径不一致）。"""
        src = _read("gui/server.py")
        self.assertNotIn("pipeline.evaluate(", src)


class TestFrontendWiring(unittest.TestCase):
    def test_insight_view_has_contract_block(self):
        src = _read("gui/assets/v2/views-insight.js")
        self.assertIn("v2-ia-contract", src)

    def test_turn_contract_initialized(self):
        """turn 创建时就要有 contract 字段（Vue 响应式要求）。"""
        src = _read("gui/assets/v2/views-insight.js")
        m = re.search(r"const turn = \{(.*?)\n        \};", src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("contract:", m.group(1))

    def test_both_result_paths_assign_contract(self):
        """轮询与 SSE 两条路都要接住 contract——漏一条就有空白页。"""
        src = _read("gui/assets/v2/views-insight.js")
        n = src.count("turn.contract = res.contract || {};")
        self.assertEqual(n, 2, f"只接住了 {n} 条路径")

    def test_contract_block_uses_backend_hint(self):
        """警告文案必须来自后端 hint，前端不自己编。"""
        src = _read("gui/assets/v2/views-insight.js")
        self.assertIn("s.status.hint", src)

    def test_stage_rows_carry_data_attributes(self):
        """阶段行要带 data 属性（端到端靠它定位）。"""
        src = _read("gui/assets/v2/views-insight.js")
        self.assertIn('data-stage-id', src)
        self.assertIn('data-status-code', src)
        self.assertIn('data-ok-to-proceed', src)

    def test_three_states_distinguished_by_class(self):
        """三态（通过/未通过/未涉及）要用 class 区分，不能只靠颜色。"""
        src = _read("gui/assets/v2/views-insight.js")
        self.assertIn("bad:", src)
        self.assertIn("skip:", src)

    def test_violated_filter_in_template(self):
        """只有 violated 的禁止项才显示。"""
        src = _read("gui/assets/v2/views-insight.js")
        self.assertIn("f.violated", src)


class TestCssWiring(unittest.TestCase):
    def test_contract_styles_exist(self):
        css = _read("gui/assets/v2/app.css")
        for sel in (".v2-ia-contract", ".v2-ia-cstage", ".v2-ia-cstage-dot",
                    ".v2-ia-cstage-code", ".v2-ia-contract-warn",
                    ".v2-ia-contract-forbid"):
            self.assertIn(sel, css, f"缺样式 {sel}")

    def test_uses_theme_tokens_not_hardcoded_colors(self):
        """走 theme.css 令牌，别写死颜色（暗色主题会瞎）。"""
        css = _read("gui/assets/v2/app.css")
        block = css[css.index(".v2-ia-contract {"):css.index(".v2-ia-steps {")]
        for line in block.splitlines():
            if ":" in line and "{" not in line:
                val = line.split(":", 1)[1]
                if val.strip().startswith("#"):
                    self.fail(f"写死了颜色：{line.strip()}")

    def test_three_states_have_distinct_dot_colors(self):
        css = _read("gui/assets/v2/app.css")
        self.assertIn(".v2-ia-cstage.bad .v2-ia-cstage-dot", css)
        self.assertIn(".v2-ia-cstage.skip .v2-ia-cstage-dot", css)

    def test_summary_uses_mono_font(self):
        """机械摘要用等宽字体——它是标识与计数，不是叙述。

        锚定**基规则**（行首就是 .v2-ia-cstage-sum），
        不是任何含这个类名的选择器——否则哪天加了个
        `.v2-ia-contract.compact .v2-ia-cstage-sum` 修饰符，
        第一个匹配就变成修饰符，检查的就不再是基规则了。
        """
        css = _read("gui/assets/v2/app.css")
        m = re.search(r"^\.v2-ia-cstage-sum\s*\{(.*?)\}", css, re.S | re.M)
        self.assertIsNotNone(m, "找不到 .v2-ia-cstage-sum 基规则")
        self.assertIn("font-mono", m.group(1))


class TestRunsWiring(unittest.TestCase):
    def test_runs_module_does_not_filter_contract_out(self):
        """runs 的摘要不该把 contract 过滤掉（详情要能读到）。"""
        src = _read("core/runs.py")
        # 摘要里可以不含 contract，但 load_run 必须原样返回
        self.assertIn("def load_run(", src)

    def test_load_run_returns_contract(self):
        src = _read("core/runs.py")
        idx = src.index("def load_run(")
        block = src[idx:idx + 600]
        self.assertNotIn("contract", block.split("return")[0].split("def load_run(")[-1]
                         .replace("contract", ""))


if __name__ == "__main__":
    unittest.main()
