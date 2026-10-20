# -*- coding: utf-8 -*-
"""心理学维度分析的行为契约。

核心纪律：**证据不足时不下结论**。这套东西最危险的失败形态不是崩溃，
是"看起来很有道理但数据不支持"——用户会当成事实去理解一个人。
所以测试重点全在"该沉默时是否沉默"、"该存疑时是否存疑"。

不需要真实数据库（用构造的 profile dict）。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.psych import (PsychAnalyzer, MIN_EVIDENCE, render_psych,  # noqa: E402
                        Evidence, Dimension)


def prof(**kw) -> dict:
    """构造一个最小 profile 结构。"""
    base = {"interaction": {}, "sentiment": {}, "signals": {}, "rhythm": {},
            "overview": {}, "topics": {}}
    base.update(kw)
    return base


class TestEvidenceGating(unittest.TestCase):
    """证据门槛：样本不够时必须返回 insufficient，不能瞎猜。"""

    def test_empty_profile_gives_no_dimensions_evidence(self):
        r = PsychAnalyzer().analyze(prof())
        for d in r["dimensions"]:
            self.assertEqual(d["confidence"], "insufficient",
                             f"{d['label']} 在没有数据时给出了结论")

    def test_below_min_evidence_is_insufficient(self):
        """样本数小于 MIN_EVIDENCE 一律 insufficient。"""
        p = prof(overview={"myMessages": 1, "theirMessages": 1},
                 interaction={"initiatedByMe": 1, "initiatedByThem": 0,
                              "myAvgReplySeconds": 10, "theirAvgReplySeconds": 100})
        r = PsychAnalyzer().analyze(p, texts_mine=["你好"], texts_theirs=["嗯"])
        for d in r["dimensions"]:
            self.assertEqual(d["confidence"], "insufficient")

    def test_has_disclaimer_always(self):
        for p in (prof(), prof(overview={"myMessages": 500})):
            r = PsychAnalyzer().analyze(p)
            self.assertIn("不是心理诊断", r["disclaimer"])


class TestAnxietyDimension(unittest.TestCase):
    def test_high_initiation_and_slow_reply_is_high_confidence(self):
        p = prof(overview={"myMessages": 80, "theirMessages": 60},
                 interaction={"initiatedByMe": 30, "initiatedByThem": 5,
                              "myAvgReplySeconds": 60, "theirAvgReplySeconds": 400})
        r = PsychAnalyzer().analyze(p)
        d = {x["key"]: x for x in r["dimensions"]}["attachment_anxiety"]
        self.assertEqual(d["confidence"], "high")
        # 必须有反例说明，否则用户会当成定论
        self.assertTrue(any(d["caveats"]))

    def test_equal_reply_speed_not_high(self):
        """双方回复速度差不多时不该判成高置信度。"""
        p = prof(overview={"myMessages": 80, "theirMessages": 60},
                 interaction={"initiatedByMe": 10, "initiatedByThem": 10,
                              "myAvgReplySeconds": 300, "theirAvgReplySeconds": 300})
        r = PsychAnalyzer().analyze(p)
        d = {x["key"]: x for x in r["dimensions"]}["attachment_anxiety"]
        self.assertNotEqual(d["confidence"], "high")


class TestLoveLanguage(unittest.TestCase):
    def test_care_words_detected_from_raw_text(self):
        """必须吃原句——用词频表匹配会得到与事实无关的结果。

        这条是回归：第一版拿 keywordsMine 去匹配词表，全是噪音，
        因为关键词是「应该」「好像」这类高频繁词，永远不含「早点睡」。
        """
        mine = ["早点睡", "记得吃饭", "别熬夜", "小心点"] * 3
        r = PsychAnalyzer().analyze(prof(overview={"myMessages": 12}),
                                    texts_mine=mine, texts_theirs=["嗯"] * 5)
        d = {x["key"]: x for x in r["dimensions"]}["love_language"]
        self.assertNotEqual(d["confidence"], "insufficient")
        top = d["evidence"][0]["behavior"]
        self.assertIn("服务的行动", top)

    def test_probing_openers_not_counted_as_affection(self):
        """「在吗」「在干嘛」是探对方在不在，不是表达好感。"""
        mine = ["在吗", "在干嘛", "在吗", "在干嘛"] * 3
        r = PsychAnalyzer().analyze(prof(overview={"myMessages": 12}),
                                    texts_mine=mine, texts_theirs=["嗯"] * 5)
        d = {x["key"]: x for x in r["dimensions"]}["love_language"]
        # 全是不算「肯定的话语」，所以要么 insufficient，要么不含该 bucket
        beh = " ".join(e["behavior"] for e in d["evidence"])
        self.assertNotIn("肯定的话语", beh)

    def test_empty_texts_gives_insufficient(self):
        r = PsychAnalyzer().analyze(prof(overview={"myMessages": 100}),
                                    texts_mine=[], texts_theirs=[])
        d = {x["key"]: x for x in r["dimensions"]}["love_language"]
        self.assertEqual(d["confidence"], "insufficient")


class TestEmotion(unittest.TestCase):
    def test_reads_positive_negative_neutral(self):
        p = prof(overview={"myMessages": 100},
                 sentiment={"theirs": {"positive": 10, "negative": 40, "neutral": 50}})
        r = PsychAnalyzer().analyze(p)
        d = {x["key"]: x for x in r["dimensions"]}["emotion_regulation"]
        self.assertNotEqual(d["confidence"], "insufficient")
        neg = [e for e in d["evidence"] if "负向" in e["behavior"]][0]
        self.assertAlmostEqual(neg["value"], 0.4, places=2)

    def test_all_zero_counts_insufficient(self):
        """三档全 0 说明没数据，不能当成"情绪平稳"。"""
        p = prof(overview={"myMessages": 100},
                 sentiment={"theirs": {"positive": 0, "negative": 0, "neutral": 0}})
        r = PsychAnalyzer().analyze(p)
        d = {x["key"]: x for x in r["dimensions"]}["emotion_regulation"]
        self.assertEqual(d["confidence"], "insufficient")


class TestEngagement(unittest.TestCase):
    def test_short_ack_rate(self):
        theirs = ["嗯"] * 30 + ["这是一段比较长的回复内容"] * 70
        r = PsychAnalyzer().analyze(prof(overview={"myMessages": 100}),
                                    texts_theirs=theirs)
        d = {x["key"]: x for x in r["dimensions"]}["engagement"]
        self.assertNotEqual(d["confidence"], "insufficient")
        short = [e for e in d["evidence"] if "短促应答" in e["behavior"]][0]
        self.assertAlmostEqual(short["value"], 0.3, places=2)

    def test_caveat_present(self):
        """短回答这一维最容易误读，必须有反例说明。"""
        r = PsychAnalyzer().analyze(prof(overview={"myMessages": 100}),
                                    texts_theirs=["嗯"] * 50)
        d = {x["key"]: x for x in r["dimensions"]}["engagement"]
        self.assertTrue(any("不代表不在意" in c for c in d["caveats"]))


class TestDimensionDataclass(unittest.TestCase):
    def test_empty_dimension_insufficient(self):
        self.assertEqual(Dimension("k", "标签").as_dict()["confidence"], "insufficient")

    def test_confidence_basis_takes_max_not_sum(self):
        """置信度基准取最大样本量，不是累加——累加会让两个小样本凑成"大样本"。"""
        d = Dimension("k", "L")
        d.evidence = [Evidence("k", "b1", 0.5, 5), Evidence("k", "b2", 0.5, 6)]
        self.assertEqual(d.as_dict()["confidenceBasis"], 6)


class TestCaveatQuality(unittest.TestCase):
    """反例必须是「针对这一条证据」的，不能一条模板套到底。

    三条证据挂同一句 caveat，页面上会重复三遍——看着像凑数，
    也让人误以为那句话是针对当前这条的。
    """

    def test_no_duplicate_caveat_within_dimension(self):
        mine = (["早点睡", "记得吃饭", "一起吃饭吧", "你真厉害"] * 4)
        theirs = (["嗯", "随便"] * 8 + ["不用了", "改天吧"] * 4
                  + ["为什么", "不想聊这个"] * 3)
        r = PsychAnalyzer().analyze(
            prof(overview={"myMessages": 200, "theirMessages": 200},
                 interaction={"initiatedByMe": 20, "initiatedByThem": 20,
                              "myAvgReplySeconds": 100, "theirAvgReplySeconds": 100},
                 sentiment={"theirs": {"positive": 10, "negative": 30, "neutral": 60}}),
            texts_mine=mine, texts_theirs=theirs)
        for d in r["dimensions"]:
            cavs = [e["caveat"] for e in d["evidence"] if e["caveat"]]
            self.assertEqual(len(cavs), len(set(cavs)),
                             f"{d['label']} 的反例有重复：{cavs}")

    def test_behavior_names_differ_within_dimension(self):
        """同一维度里两条证据的说明不能一模一样，否则用户分不清在说什么。"""
        theirs = ["嗯"] * 20 + ["不用了"] * 10 + ["为什么"] * 5
        r = PsychAnalyzer().analyze(prof(overview={"myMessages": 100}),
                                    texts_theirs=theirs)
        for d in r["dimensions"]:
            behs = [e["behavior"] for e in d["evidence"]]
            self.assertEqual(len(behs), len(set(behs)),
                             f"{d['label']} 的证据说明重复：{behs}")


class TestValueFormatting(unittest.TestCase):
    """数字显示口径：比例 vs 倍数不能混，小值不能夸大。"""

    def setUp(self):
        from core.psych import _fmt_ev
        self.f = _fmt_ev

    def test_ratio_becomes_percent(self):
        self.assertEqual(self.f({"behavior": "我方主动开启的比例", "value": 0.783}), "78.3%")
        self.assertEqual(self.f({"behavior": "负向情绪占比", "value": 0.038}), "3.8%")

    def test_multiplier_not_shown_as_percent(self):
        """1.47 是倍数，显示成 147% 会直接读错。"""
        self.assertEqual(
            self.f({"behavior": "对方平均回复耗时 / 我方平均回复耗时", "value": 1.47}), "1.47")

    def test_tiny_ratio_not_inflated(self):
        """0.0006*100=0.06 若先 round 会变成 0.1%，夸大十倍。"""
        self.assertEqual(self.f({"behavior": "拒绝性措辞占比", "value": 0.0006}), "<0.1%")
        self.assertEqual(self.f({"behavior": "占比", "value": 0.00001}), "<0.1%")

    def test_just_above_threshold(self):
        self.assertEqual(self.f({"behavior": "占比", "value": 0.0038}), "0.4%")


class TestRender(unittest.TestCase):
    def test_render_handles_empty(self):
        self.assertIn("样本不足", render_psych({"dimensions": []}))

    def test_render_includes_caveats(self):
        r = PsychAnalyzer().analyze(
            prof(overview={"myMessages": 100, "theirMessages": 100},
                 interaction={"initiatedByMe": 50, "initiatedByThem": 5,
                              "myAvgReplySeconds": 60, "theirAvgReplySeconds": 500}))
        md = render_psych(r)
        self.assertIn("反例可能", md)
        self.assertIn("不是心理诊断", md)


if __name__ == "__main__":
    unittest.main()
