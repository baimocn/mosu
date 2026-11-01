# -*- coding: utf-8 -*-
"""迭代 10：证据账本升级。

背景（对标 Lancer 的 OrchestrationEvidenceRecord）：
    Lancer 的证据账本有 4 个正交维度 —— kind(7 种) / source(7 种) /
    status(6 种) / severity(P0-P3)。墨溯原本只有 kind 的 2 种
    （quote / stat），且连 stat 的口径都没有。

本轮补的（有意为之，不是照抄）：
    ① source    —— 墨溯版按「数据产出链路」分 4 种（db/message/agent/model），
                    不抄 Lancer 的编排语义（worker-reported / orchestrator-accepted…）
    ② scope    —— 数字的口径（统计对象 / 时间范围 / 统计维度）。**Lancer 没有**，
                    但这是墨溯最实的缺口：「消息量 158565 条」用户没法核对
    ③ status   —— 墨溯版只有 verified / reported 两种，
                    语义是「能不能被程序化核验」，不是 Lancer 的编排生命周期
    ④ severity —— **刻意不抄**。Lancer 的 P0-P3 是「代码审查发现有多严重」，
                    聊天分析里没有对应物，硬套是形式主义
    ⑤ 数字审计分池 —— 主代理实测 vs 子代理转述
    ⑥ 推断标注 —— 模型的判断要能跟「查出来的事实」分开摆
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agent_engine, agent_tools, pipeline  # noqa: E402


# ------------------------------------------------------------------ 工具层

class TestEvidenceShape(unittest.TestCase):
    """每条证据都必须带 source / status / scope 三件套。"""

    def test_ev_factory_sets_three_fields(self):
        box = agent_tools.ToolBox.__new__(agent_tools.ToolBox)
        ev = agent_tools.ToolBox._ev(
            box, "stat", "消息量 100 条", source=agent_tools.SOURCE_DB,
            scope=agent_tools.scope_of(scope="全库", period="全部时间",
                                       metric="消息量排行"))
        self.assertEqual(ev["kind"], "stat")
        self.assertEqual(ev["source"], "db")
        self.assertEqual(ev["status"], "verified")
        self.assertEqual(ev["scope"]["metric"], "消息量排行")

    def test_ev_factory_defaults_scope(self):
        """没给 scope 也要有 scope 字段（空结构），不能是 None。"""
        box = agent_tools.ToolBox.__new__(agent_tools.ToolBox)
        ev = agent_tools.ToolBox._ev(box, "stat", "x",
                                     source=agent_tools.SOURCE_DB)
        self.assertIsInstance(ev["scope"], dict)
        self.assertEqual(ev["scope"]["scope"], "")

    def test_quote_and_stat_are_verified(self):
        box = agent_tools.ToolBox.__new__(agent_tools.ToolBox)
        for kind in ("quote", "stat"):
            ev = agent_tools.ToolBox._ev(box, kind, "x",
                                         source=agent_tools.SOURCE_DB)
            self.assertEqual(ev["status"], agent_tools.STATUS_VERIFIED)

    def test_inference_kind_is_reported(self):
        """推断类结论标 reported——它没有可核验的载体。"""
        box = agent_tools.ToolBox.__new__(agent_tools.ToolBox)
        ev = agent_tools.ToolBox._ev(box, "inference", "他可能在疏远你",
                                     source=agent_tools.SOURCE_MODEL)
        self.assertEqual(ev["status"], agent_tools.STATUS_REPORTED)

    def test_scope_text_joins_parts(self):
        sc = agent_tools.scope_of(scope="全库", period="2025-01-01 ~ 2025-06-01",
                                  metric="消息量排行")
        txt = agent_tools.scope_text(sc)
        self.assertIn("全库", txt)
        self.assertIn("消息量排行", txt)

    def test_scope_text_skips_empty(self):
        sc = agent_tools.scope_of(scope="全库")
        self.assertEqual(agent_tools.scope_text(sc), "全库")

    def test_source_constants_four_kinds(self):
        """墨溯版的 source 是 4 种（db/message/agent/model）。"""
        self.assertEqual(
            {agent_tools.SOURCE_DB, agent_tools.SOURCE_MESSAGE,
             agent_tools.SOURCE_AGENT, agent_tools.SOURCE_MODEL},
            {"db", "message", "agent", "model"})

    def test_status_constants_two_kinds(self):
        """墨溯版的 status 只有两种——不抄 Lancer 的 6 种编排状态。"""
        self.assertEqual({agent_tools.STATUS_VERIFIED,
                          agent_tools.STATUS_REPORTED},
                         {"verified", "reported"})


# ------------------------------------------------------------------ 数字审计分池

def _run(answer="", tool_numbers=None, branch_numbers=None, evidence=None):
    run = agent_engine.AgentRun("q")
    run.answer = answer
    run.tool_numbers = set(tool_numbers or ())
    run.branch_numbers = set(branch_numbers or ())
    run.evidence = list(evidence or ())
    return run


class TestNumberAuditPools(unittest.TestCase):
    """主代理实测 vs 子代理转述，必须分得开。"""

    def test_all_numbers_is_union(self):
        r = _run(tool_numbers={"10"}, branch_numbers={"20"})
        self.assertEqual(r.all_numbers, {"10", "20"})

    def test_measured_number_has_no_audit_note(self):
        r = _run(answer="共 100 条。", tool_numbers={"100"})
        self.assertEqual(agent_engine._unsourced_numbers(r), [])
        self.assertEqual(agent_engine._transcribed_numbers(r), [])
        self.assertNotIn("核对提醒", agent_engine._audit_numbers(r))

    def test_branch_number_not_unsourced(self):
        """子代理的数字**不算**没出处——它有出处，只是转述来的。

        这条不能反：反了等于冤枉子代理，也等于让「派子代理」这个能力
        变成「用了就会被审计报警」。

        （用 158565 而不是 100：100 在 _NUM_ALLOW 白名单里，
        会被当成百分比豁免，测不出分池逻辑。）
        """
        r = _run(answer="共 158565 条。", branch_numbers={"158565"})
        self.assertEqual(agent_engine._unsourced_numbers(r), [])
        self.assertEqual(agent_engine._transcribed_numbers(r), ["158565"])

    def test_branch_number_gets_transcribed_note(self):
        r = _run(answer="共 158565 条。", branch_numbers={"158565"})
        out = agent_engine._audit_numbers(r)
        self.assertIn("出处说明", out)
        self.assertIn("子代理", out)
        self.assertNotIn("没找到出处", out)

    def test_measured_beats_branch(self):
        """同一个数字主代理也实测过 → 不提转述（实测档次更高）。"""
        r = _run(answer="共 158565 条。", tool_numbers={"158565"},
                 branch_numbers={"158565"})
        self.assertEqual(agent_engine._transcribed_numbers(r), [])

    def test_no_source_number_is_unsourced(self):
        r = _run(answer="共 29043 条。")
        self.assertEqual(agent_engine._unsourced_numbers(r), ["29043"])
        self.assertIn("核对提醒", agent_engine._audit_numbers(r))

    def test_unsourced_note_and_transcribed_note_coexist(self):
        r = _run(answer="共 29043 条和 158565 条。", branch_numbers={"158565"})
        out = agent_engine._audit_numbers(r)
        self.assertIn("核对提醒", out)
        self.assertIn("出处说明", out)

    def test_audit_does_not_mutate_answer_numbers(self):
        """审计提示不能篡改答案的数字（提示里的数字不该进审计范围）。"""
        r = _run(answer="共 158565 条。")
        before = r.answer
        agent_engine._audit_numbers(r)
        self.assertEqual(r.answer, before)


# ------------------------------------------------------------------ 推断标注

class TestInferenceMarks(unittest.TestCase):
    """模型的判断要能跟「查出来的事实」分开。"""

    def test_inference_sentence_detected(self):
        r = _run(answer="他可能在疏远你。这几天回复变慢了。")
        marks = agent_engine._inference_marks(r)
        self.assertTrue(any("疏远" in m for m in marks))
        self.assertFalse(any("回复变慢" in m for m in marks))

    def test_factual_sentence_not_marked(self):
        r = _run(answer="他最近回复变慢了。")
        self.assertEqual(agent_engine._inference_marks(r), [])

    def test_sentence_with_number_not_marked(self):
        """带数字的句子有可核验载体，不该被当成纯推断。"""
        r = _run(answer="他可能发了 3 次消息。")
        self.assertEqual(agent_engine._inference_marks(r), [])

    def test_sentence_with_quote_not_marked(self):
        """带引号引用的句子是事实陈述，不该被当成纯推断。"""
        r = _run(answer="他可能说过「懒得聊」。")
        self.assertEqual(agent_engine._inference_marks(r), [])

    def test_inference_note_in_audit(self):
        r = _run(answer="他可能在疏远你。")
        out = agent_engine._audit_numbers(r)
        self.assertIn("判断说明", out)
        self.assertIn("主观判断", out)

    def test_no_inference_no_note(self):
        r = _run(answer="共 100 条消息。", tool_numbers={"100"})
        out = agent_engine._audit_numbers(r)
        self.assertNotIn("判断说明", out)

    def test_inference_marks_ignore_short_fragments(self):
        r = _run(answer="可能。")
        self.assertEqual(agent_engine._inference_marks(r), [])


# ------------------------------------------------------------------ 分支证据

class TestBranchEvidence(unittest.TestCase):
    """子代理证据要标 agent 来源 + 转述标记。"""

    class _Br:
        def __init__(self, ev):
            self.id = "b1"
            self.goal = "查张三的聊天"
            self.evidence = ev

    def test_branch_evidence_gets_source(self):
        br = self._Br([{"kind": "quote", "text": "在忙吗"}])
        out = agent_engine._branch_evidence([br])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], agent_tools.SOURCE_AGENT)
        self.assertEqual(out[0]["via"], "查张三的聊天")
        self.assertEqual(out[0]["branchId"], "b1")

    def test_branch_keeps_message_source_but_marks_transcribed(self):
        """子代理查到的原话仍是原话（能核对），但要标出是转述来的。"""
        br = self._Br([{"kind": "quote", "text": "在忙吗",
                        "source": agent_tools.SOURCE_MESSAGE}])
        out = agent_engine._branch_evidence([br])
        self.assertEqual(out[0]["source"], agent_tools.SOURCE_MESSAGE)
        self.assertTrue(out[0]["transcribed"])

    def test_branch_evidence_no_transcribed_flag_for_stat(self):
        br = self._Br([{"kind": "stat", "text": "100 条",
                        "source": agent_tools.SOURCE_DB}])
        out = agent_engine._branch_evidence([br])
        self.assertNotIn("transcribed", out)


# ------------------------------------------------------------------ 契约

class TestScopeContract(unittest.TestCase):
    """新增 forbidden：数字证据必须带口径。"""

    def _run(self, **kw):
        base = {"steps": [{"tool": "chat_stats"}],
                "evidence": [{"kind": "stat", "text": "消息量 100 条"}],
                "answer": "他发了很多。"}
        base.update(kw)
        return base

    def test_stat_without_scope_violates(self):
        r = pipeline.evaluate(self._run())
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertTrue(f["no_stat_without_scope"]["violated"])

    def test_stat_with_scope_no_violation(self):
        r = pipeline.evaluate(self._run(evidence=[{
            "kind": "stat", "text": "消息量 100 条",
            "scope": {"scope": "全库", "period": "全部时间",
                      "metric": "消息量排行"}}]))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertFalse(f["no_stat_without_scope"]["violated"])

    def test_string_scope_accepted(self):
        """兼容旧形态：scope 是字符串。"""
        r = pipeline.evaluate(self._run(evidence=[{
            "kind": "stat", "text": "消息量 100 条", "scope": "全库"}]))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertFalse(f["no_stat_without_scope"]["violated"])

    def test_quote_without_scope_not_violated(self):
        """只有数字证据受这条约束——原话不需要口径。"""
        r = pipeline.evaluate(self._run(evidence=[{
            "kind": "quote", "text": "在忙吗"}]))
        f = {x["id"]: x for x in r["forbidden"]}
        self.assertFalse(f["no_stat_without_scope"]["violated"])


# ------------------------------------------------------------------ 诚实性

class TestNoSeveritySneakedIn(unittest.TestCase):
    """刻意不抄 Lancer 的 severity（P0-P3）。

    这条测试的作用是**防止后来者"顺手补上"**——
    看着 Lancer 有 severity 就想加，是典型的形式主义抄作业。
    """

    def test_no_severity_field_in_evidence_factory(self):
        box = agent_tools.ToolBox.__new__(agent_tools.ToolBox)
        ev = agent_tools.ToolBox._ev(box, "stat", "x",
                                     source=agent_tools.SOURCE_DB)
        self.assertNotIn("severity", ev)

    def test_no_severity_in_module(self):
        self.assertFalse(hasattr(agent_tools, "SEVERITY"))
        self.assertFalse(hasattr(agent_engine, "SEVERITY"))


if __name__ == "__main__":
    unittest.main()


class TestNonTextPlaceholder(unittest.TestCase):
    """非文本消息必须产出**可读**的证据，不能是空白条目。

    背景：端到端跑出 212 条 quote，其中 16 条 text 和 full 都是空的——
    表情/图片/语音的正文在库里本来就是空的。空白证据点开什么也看不到，
    「可核对」就成了空话。
    """

    def test_msg_text_uses_body_when_present(self):
        from core.agent_tools import _msg_text
        self.assertEqual(_msg_text({"content": "在吗", "kind": "text"}), "在吗")

    def test_msg_text_placeholder_for_emoji(self):
        from core.agent_tools import _msg_text
        self.assertEqual(_msg_text({"content": "", "kind": "emoji"}), "[表情]")

    def test_msg_text_placeholder_for_image(self):
        from core.agent_tools import _msg_text
        self.assertEqual(_msg_text({"content": "", "kind": "image"}), "[图片]")

    def test_msg_text_voice_carries_duration(self):
        from core.agent_tools import _msg_text
        got = _msg_text({"content": "", "kind": "voice", "voiceMs": 3200})
        self.assertIn("语音", got)
        self.assertIn("3", got)

    def test_msg_text_file_carries_name(self):
        from core.agent_tools import _msg_text
        got = _msg_text({"content": "", "kind": "file",
                         "fileName": "方案.pdf"})
        self.assertIn("方案.pdf", got)

    def test_msg_text_never_returns_empty(self):
        from core.agent_tools import _msg_text
        for kind in ("emoji", "image", "video", "voice", "file",
                     "system", "other", "", None):
            got = _msg_text({"content": "", "kind": kind})
            self.assertTrue(got.strip(), f"kind={kind} 产出了空文本")

    def test_whitespace_only_body_is_treated_as_empty(self):
        from core.agent_tools import _msg_text
        got = _msg_text({"content": "   \n  ", "kind": "emoji"})
        self.assertEqual(got, "[表情]")

    def test_timeline_evidence_never_blank(self):
        """真跑一次时间线工具，确认没有空白证据。"""
        try:
            from core.dataset import Dataset
            from core.agent_tools import ToolBox
            ds = Dataset()
            box = ToolBox(ds)
            sess = [s for s in ds.sessions()
                    if s.get("type") == "private"
                    and (s.get("messageCount") or 0) > 50]
            if not sess:
                self.skipTest("本机没有私聊会话")
            sess.sort(key=lambda s: -(s.get("messageCount") or 0))
            r = box.run("read_timeline",
                        {"sessionId": sess[0].get("sessionId"), "limit": 120})
            ev = r.get("evidence") or []
            if not ev:
                self.skipTest("没有证据产出")
            blank = [e for e in ev
                     if not str(e.get("text") or "").strip()
                     or not str(e.get("full") or "").strip()]
            self.assertEqual(blank, [], f"{len(blank)} 条空白证据")
        except (Exception, SystemExit) as e:   # noqa: BLE001
            # SystemExit 也要接：Dataset() 找不到微信数据目录时是 raise SystemExit，
            # 不是普通异常（不接的话单测会红，而这不是代码的问题）。
            self.skipTest(f"环境不可用：{e}")


class TestInferenceDetectionAgainstRealAnswers(unittest.TestCase):
    """推断判据必须按**真实答案样本**验，不能只靠造的用例。

    背景：迭代 10 第一版词表只有 12 个词（可能/也许/感觉/推测…），
    端到端实跑整轮答案**一句都没标出来**——模型实际写的是
    「整体是」「这也解释了」「这是典型的」「没看到…的迹象」，
    一个都不在词表里。判据看着有、实际不工作。
    """

    def _marks(self, text: str) -> list[str]:
        from core.agent_engine import AgentRun, _inference_marks
        run = AgentRun(question="q")
        run.answer = text
        return _inference_marks(run)

    def test_marks_conclusion_style_phrases(self):
        """真实答案里的结论性措辞必须被标出。"""
        cases = [
            "整体是你提供东西多、他索取和咨询多",
            "这也解释了为什么消息数他更多，但话题主导权在你这边",
            "没看到疏远的迹象，也谈不上热络",
            "他可能在疏远你",
            "这是典型的有事就找型：学期中密集，假期静默",
        ]
        for text in cases:
            got = self._marks(text)
            self.assertTrue(got, f"漏标：{text}")

    def test_does_not_mark_factual_statements(self):
        """有数字的事实陈述不能被标（标了就是错标）。"""
        cases = [
            "10-22 08:45 他说我基本上都没去",
            "2026-06 有 377 条，是峰值",
            "他发了 1805 条，我发了 1365 条",
        ]
        for text in cases:
            got = self._marks(text)
            self.assertEqual(got, [], f"错标：{text} → {got}")

    def test_does_not_mark_clarifying_questions(self):
        """澄清提问不是判断——上一版把「你说一下大概是什么内容」误标了。

        错标比漏标更伤：它告诉用户「这是模型的猜测」，
        但模型只是在正常反问。
        """
        got = self._marks("你说一下大概是什么内容，我直接按词去捞")
        self.assertEqual(got, [], f"错标：{got}")

    def test_does_not_mark_quoted_original_words(self):
        """引原话 = 有可核验载体，不该标。"""
        got = self._marks("他说「懒得聊」，说了3次")
        self.assertEqual(got, [], f"错标：{got}")

    def test_marks_conclusion_with_rhetorical_quote(self):
        """引号当修辞用（不是引原话）时，结论句仍要标。"""
        got = self._marks("这是典型的「有事就找」型：学期中密集")
        self.assertTrue(got, "带修辞引号的结论句被漏标")

    def test_marks_self_declared_judgement(self):
        """「我的判断：…」是模型最爱用的推断标记，必须能标出。

        这一条漏了整整两版——模型写「我的判断」，
        而第一版词表里只有「可能/也许/感觉」，压根没有「判断」。
        """
        got = self._marks(
            "**我的判断**：他对你不算冷淡，属于「有事说事」的搭子")
        self.assertTrue(got, "漏标：模型自报的「我的判断」")

    def test_marks_hedged_conclusion(self):
        got = self._marks("但这只是最后一条记录，不能因此断定他态度转冷")
        self.assertTrue(got, "漏标：「不能因此断定」类限定判断")

    def test_does_not_mark_quote_containing_shuo(self):
        """引号里的词含「说」字（如「有事说事」）不算引原话。

        「属于「有事说事」的搭子」里有「说」，但那是引号**里面**的词，
        不是引用动词——所以这句该标。
        """
        got = self._marks("我的判断：他属于「有事说事」的搭子")
        self.assertTrue(got, "漏标：引号当修辞用的结论句")

    def test_inference_words_have_no_position_constraint(self):
        """词表里不能出现「这解释了」这种带位置约束的写法。

        模型写的是「这也解释了」，中间插个「也」就匹配不上——
        词表带位置约束必然漏检。
        """
        from core.agent_engine import _INFER_WORDS
        for w in _INFER_WORDS:
            self.assertLessEqual(len(w), 4,
                                 f"词「{w}」过长，可能带位置约束导致漏检")
