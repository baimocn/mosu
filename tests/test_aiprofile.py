# -*- coding: utf-8 -*-
"""AI 人格分析层（core/aiprofile.py）的离线测试。

全部用 FakeLlm 注入，零网络、零花费、零真实数据外发。
锁的是四条纪律：引文锚定（防编造）、禁诊断词、置信度纪律、无配置优雅降级。
"""
import json
import os
import unittest


class FakeLlm:
    """按脚本依次返回的假 LLM；calls 计数供缓存断言。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.model = "fake-model"

    def chat(self, messages, temperature=0.8, max_tokens=300):
        self.calls += 1
        return self.replies.pop(0) if self.replies else "{}"


class FakeDS:
    """最小 Dataset 替身：demo=False + 空 sessions（签名稳定，缓存可命中）。"""

    demo = False

    def sessions(self):
        return []


def _samples():
    return [
        {"uid": "m0", "ts": "03-15 14:02", "dir": "我", "text": "今晚月色很好，出来走走？"},
        {"uid": "m1", "ts": "03-16 09:10", "dir": "我", "text": "方案我改了三次，客户还是说要再简洁一点"},
        {"uid": "m2", "ts": "03-17 21:44", "dir": "我", "text": "别熬夜了，明天还要早起开会"},
        {"uid": "t0", "ts": "03-15 14:05", "dir": "对方", "text": "好，老地方见"},
        {"uid": "t1", "ts": "03-16 09:30", "dir": "对方", "text": "辛苦了，慢慢改不着急"},
    ]


GOOD_REPLY = {
    "summary": "主动性较强，推进事情不拖沓，同时会照顾对方状态。",
    "dimensions": [
        {"key": "personality", "label": "性格特质",
         "assessment": "回应时愿意给出增量信息，不敷衍",
         "confidence": "high",
         "evidence": [
             {"uid": "t0", "quote": "好，老地方见", "analysis": "答应痛快"},
             {"uid": "t1", "quote": "辛苦了，慢慢改不着急", "analysis": "体谅对方"}],
         "counterpoints": ["也可能只是性格外向，看人下菜"]},
        {"key": "emotion", "label": "情绪模式",
         "assessment": "会留意对方状态",
         "confidence": "high",          # 只有 1 条证据 → 必须被压到 medium
         "evidence": [
             {"uid": "t1", "quote": "辛苦了，慢慢改不着急", "analysis": "安抚语气"}],
         "counterpoints": []},
    ],
}


def _profile(scope="contact"):
    base = {"scope": scope,
            "session": {"sessionId": "wxid_test", "displayName": "测试会话"},
            "overview": {"totalMessages": 100, "myMessages": 50, "theirMessages": 50,
                         "myShare": 0.5, "activeDays": 30, "spanDays": 10.0,
                         "firstText": "03-01", "lastText": "03-20"},
            "interaction": {"initiatedByMe": 12, "initiatedByThem": 8,
                            "myAvgReplySeconds": 120, "theirAvgReplySeconds": 300},
            "sentiment": {"mine": {"avg": 0.2, "positive": 20, "negative": 5, "neutral": 25},
                          "theirs": {"avg": 0.3, "positive": 22, "negative": 3, "neutral": 25}},
            "signals": {"emojiRate": 0.1, "laughPer100": 3.2,
                        "lengthTrend": "持平", "mediaShare": 0.15},
            "psych": {"dimensions": []}}
    return base


class TestParseAndValidate(unittest.TestCase):
    def setUp(self):
        from core import aiprofile
        aiprofile._cache().clear()

    def test_fence_json_parsed(self):
        from core.aiprofile import _parse_json
        raw = "```json\n" + json.dumps(GOOD_REPLY, ensure_ascii=False) + "\n```"
        self.assertEqual(_parse_json(raw)["summary"], GOOD_REPLY["summary"])

    def test_json_with_surrounding_prose(self):
        from core.aiprofile import _parse_json
        raw = "好的，结果如下：\n" + json.dumps(GOOD_REPLY, ensure_ascii=False) + "\n以上。"
        self.assertIsNotNone(_parse_json(raw))

    def test_garbage_returns_none(self):
        from core.aiprofile import _parse_json
        self.assertIsNone(_parse_json("完全不是 JSON"))

    def test_reasoning_prefix_parsed(self):
        """推理模型把思考过程吐进 content：JSON 埋在独白后面也要能挖出来。
        deepseek-v4-flash 实测就会这样（见真机联调记录）。"""
        from core.aiprofile import _parse_json
        raw = ('先看看这个样本。target maybe not uncle? 若风 is contact name. '
               'The message t15 says 二叔。Wait if target is 若风...\n'
               + json.dumps(GOOD_REPLY, ensure_ascii=False))
        out = _parse_json(raw)
        self.assertIsNotNone(out)
        self.assertEqual(out["summary"], GOOD_REPLY["summary"])

    def test_json_after_trailing_prose(self):
        from core.aiprofile import _parse_json
        raw = json.dumps(GOOD_REPLY, ensure_ascii=False) + "\n以上是分析结果，希望对你有帮助。"
        self.assertIsNotNone(_parse_json(raw))

    def test_valid_reply_kept(self):
        from core.aiprofile import validate
        out = validate(GOOD_REPLY, _samples(), "contact")
        labels = [d["label"] for d in out["dimensions"]]
        self.assertIn("性格特质", labels)
        self.assertEqual(len(out["dropped"]), 0)
        self.assertGreater(len(out["summary"]), 0)

    def test_fabricated_quote_dropped(self):
        """引文在样本里不存在 → 证据整条丢弃，维度降 insufficient。"""
        from core.aiprofile import validate
        bad = {"summary": "x", "dimensions": [
            {"key": "personality", "label": "性格特质", "assessment": "他很外向",
             "confidence": "high",
             "evidence": [{"uid": "m0", "quote": "这句原话根本不存在于样本中",
                           "analysis": "编造的"}],
             "counterpoints": []}]}
        out = validate(bad, _samples(), "contact")
        self.assertEqual(out["dimensions"], [])
        self.assertIn("性格特质", out["insufficient"])

    def test_nonexistent_uid_dropped(self):
        from core.aiprofile import validate
        bad = {"summary": "x", "dimensions": [
            {"key": "personality", "label": "性格特质", "assessment": "x",
             "confidence": "high",
             "evidence": [{"uid": "m99", "quote": "辛苦了，慢慢改不着急",
                           "analysis": "x"}],
             "counterpoints": []}]}
        out = validate(bad, _samples(), "contact")
        # 原话本身真实存在（对方方向）→ 全局匹配兜底救回，但 note 标注 uid 失效
        self.assertEqual(len(out["dimensions"]), 1)
        self.assertIn("不存在", out["dimensions"][0]["evidence"][0]["note"])

    def test_diagnostic_dimension_dropped(self):
        """出现诊断性用语 → 整维丢弃，不降级不保留。"""
        from core.aiprofile import validate
        bad = {"summary": "x", "dimensions": [
            {"key": "emotion", "label": "情绪模式", "assessment": "他有抑郁倾向",
             "confidence": "high",
             "evidence": [{"uid": "m2", "quote": "别熬夜了，明天还要早起开会",
                           "analysis": "x"}],
             "counterpoints": []}]}
        out = validate(bad, _samples(), "contact")
        self.assertEqual(out["dimensions"], [])
        self.assertTrue(any("诊断" in d["reason"] for d in out["dropped"]))

    def test_confidence_capped_by_evidence_count(self):
        """1 条证据自称 high → 压成 medium；0 条 → insufficient。"""
        from core.aiprofile import validate
        out = validate(GOOD_REPLY, _samples(), "contact")
        by_label = {d["label"]: d for d in out["dimensions"]}
        self.assertEqual(by_label["情绪模式"]["confidence"], "medium")

    def test_unknown_dimension_dropped(self):
        from core.aiprofile import validate
        bad = {"summary": "x", "dimensions": [
            {"key": "astrology", "label": "星座", "assessment": "x",
             "confidence": "high",
             "evidence": [{"uid": "m0", "quote": "今晚月色很好，出来走走？",
                           "analysis": "x"}],
             "counterpoints": []}]}
        out = validate(bad, _samples(), "contact")
        self.assertEqual(out["dimensions"], [])
        self.assertTrue(any("未知维度" in d["reason"] for d in out["dropped"]))

    def test_dice_helper(self):
        from core.aiprofile import _dice
        same = _dice("今晚月色很好", "今晚月色很好")
        self.assertEqual(same, 1.0)
        diff = _dice("今晚月色很好", "明天天气不错")
        self.assertLess(diff, 0.3)

    def test_direction_mismatch_dropped(self):
        """描述对方的维度只引本人消息 → 整维丢弃。

        真机联调实测：deepseek 把本人的「没有」「6个人」当成若风的说话
        风格，整个判断张冠李戴。单句 prompt 提醒靠不住，校验器硬拦。
        """
        from core.aiprofile import validate
        bad = {"summary": "x", "dimensions": [
            {"key": "personality", "label": "性格特质",
             "assessment": "对方回复短、事实优先",
             "confidence": "high",
             "evidence": [
                 {"uid": "m0", "quote": "今晚月色很好，出来走走？", "analysis": "x"},
                 {"uid": "m1", "quote": "方案我改了三次，客户还是说要再简洁一点", "analysis": "x"}],
             "counterpoints": []}]}
        out = validate(bad, _samples(), "contact")
        self.assertEqual(out["dimensions"], [])
        self.assertTrue(any("方向" in d["reason"] for d in out["dropped"]))

    def test_mixed_evidence_kept(self):
        """有对方方向证据的维度不误伤（关系/互动类维度本就混合双方）。"""
        from core.aiprofile import validate
        ok = {"summary": "x", "dimensions": [
            {"key": "personality", "label": "性格特质", "assessment": "务实",
             "confidence": "medium",
             "evidence": [
                 {"uid": "t0", "quote": "好，老地方见", "analysis": "x"},
                 {"uid": "m0", "quote": "今晚月色很好，出来走走？", "analysis": "x"}],
             "counterpoints": []}]}
        out = validate(ok, _samples(), "contact")
        self.assertEqual(len(out["dimensions"]), 1)

    def test_self_scope_no_direction_rule(self):
        """self 画像全部是本人消息，不该触发方向规则。"""
        from core.aiprofile import validate
        out = validate(GOOD_REPLY, _samples(), "self")
        self.assertEqual(len(out["dimensions"]), 2)


class TestPickSamples(unittest.TestCase):
    def test_uids_unique_and_dir_split(self):
        from core.aiprofile import pick_samples
        msgs = [{"ts": 100 + i, "direction": "out" if i % 2 else "in",
                 "text": f"消息内容号码{i}号", "session": "s1"} for i in range(20)]
        out = pick_samples(msgs, "contact", per_side=90)
        uids = [s["uid"] for s in out]
        self.assertEqual(len(uids), len(set(uids)))
        self.assertTrue(any(u.startswith("m") for u in uids))
        self.assertTrue(any(u.startswith("t") for u in uids))

    def test_self_scope_only_mine(self):
        from core.aiprofile import pick_samples
        msgs = [{"ts": 100 + i, "direction": "out", "text": f"内容{i}", "session": "s1"}
                for i in range(10)]
        msgs += [{"ts": 200 + i, "direction": "in", "text": f"对方{i}", "session": "s1"}
                 for i in range(10)]
        out = pick_samples(msgs, "self", per_side=90)
        self.assertTrue(all(s["dir"] == "我" for s in out))

    def test_contact_sorted_by_time(self):
        from core.aiprofile import pick_samples
        msgs = [{"ts": 100, "direction": "in", "text": "对方先说", "session": "s1"},
                {"ts": 200, "direction": "out", "text": "我后说", "session": "s1"}]
        out = pick_samples(msgs, "contact", per_side=90)
        self.assertEqual([s["uid"] for s in out], ["t0", "m0"])


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        from core import aiprofile
        aiprofile._cache().clear()

    def test_no_llm_configured_degrades(self):
        """没配大模型 → available False，analyze 返回降级 dict，不抛异常。

        注意：项目根的 .env（已 gitignore）会被 core.bot.llm 的 _load_dotenv
        在 import 时加载。必须先触发 import 再摘环境变量，否则测试会读到
        开发者本机的真实 key。
        """
        import core.bot.llm  # noqa: F401  —— 先让 .env 加载完
        from core.aiprofile import AiProfileAnalyzer
        from unittest.mock import patch
        for k in ("MOSU_BOT_API_KEY", "MOSU_BOT_BASE_URL", "MOSU_BOT_MODEL"):
            os.environ.pop(k, None)
        # keyvault 链（MOSU_HOME → 旧版 ~/.miyu 自动发现）也要掐断，
        # 否则测试会读到开发者本机真实配置
        with patch("core.llmconfig.load_llm_config",
                   return_value={"baseUrl": "", "apiKey": "", "model": "",
                                 "temperature": 0.8}):
            an = AiProfileAnalyzer(ds=FakeDS())
            self.assertFalse(an.available())
            out = an.analyze(_profile(), [], scope="contact")
        self.assertFalse(out["available"])
        self.assertIn("未配置", out["reason"])

    def test_demo_mode_skips_ai(self):
        from core.aiprofile import AiProfileAnalyzer

        class DemoDS(FakeDS):
            demo = True
        an = AiProfileAnalyzer(ds=DemoDS(), llm=FakeLlm([json.dumps(GOOD_REPLY)]))
        out = an.analyze(_profile(), _samples_as_msgs(), scope="contact")
        self.assertFalse(out["available"])
        self.assertIn("演示模式", out["reason"])

    def test_insufficient_samples(self):
        from core.aiprofile import AiProfileAnalyzer
        an = AiProfileAnalyzer(ds=FakeDS(), llm=FakeLlm(["{}"]))
        msgs = [{"ts": 1, "direction": "out", "text": "只有一条"}]
        out = an.analyze(_profile(), msgs, scope="self")
        self.assertFalse(out["available"])
        self.assertIn("样本不足", out["reason"])

    def test_happy_path(self):
        from core.aiprofile import AiProfileAnalyzer
        an = AiProfileAnalyzer(ds=FakeDS(), llm=FakeLlm([json.dumps(GOOD_REPLY)]))
        out = an.analyze(_profile(), _samples_as_msgs(), scope="contact")
        self.assertTrue(out["available"])
        self.assertTrue(any(d["label"] == "性格特质" for d in out["dimensions"]))
        self.assertEqual(out["model"], "fake-model")

    def test_retry_on_bad_json(self):
        """第一次返回垃圾 → 自动带反馈重试 → 第二次成功。"""
        from core.aiprofile import AiProfileAnalyzer
        llm = FakeLlm(["完全不是JSON", json.dumps(GOOD_REPLY)])
        an = AiProfileAnalyzer(ds=FakeDS(), llm=llm)
        out = an.analyze(_profile(), _samples_as_msgs(), scope="contact")
        self.assertTrue(out["available"])
        self.assertEqual(llm.calls, 2)

    def test_cache_prevents_second_llm_call(self):
        from core.aiprofile import AiProfileAnalyzer
        llm = FakeLlm([json.dumps(GOOD_REPLY), json.dumps(GOOD_REPLY)])
        an = AiProfileAnalyzer(ds=FakeDS(), llm=llm)
        args = dict(profile=_profile(), msgs=_samples_as_msgs(),
                    scope="contact", session_key="wxid_test")
        first = an.analyze(**args)
        second = an.analyze(**args)
        self.assertEqual(llm.calls, 1)
        self.assertTrue(second["cached"])
        self.assertTrue(first["available"])

    def test_env_kill_switch(self):
        from core.aiprofile import AiProfileAnalyzer
        an = AiProfileAnalyzer(ds=FakeDS(), llm=FakeLlm(["{}"]))
        os.environ["MOSU_PROFILE_AI"] = "0"
        try:
            self.assertFalse(an.available())
        finally:
            os.environ.pop("MOSU_PROFILE_AI", None)

    def test_render_markdown(self):
        from core.aiprofile import AiProfileAnalyzer, render_ai
        an = AiProfileAnalyzer(ds=FakeDS(), llm=FakeLlm([json.dumps(GOOD_REPLY)]))
        out = an.analyze(_profile(), _samples_as_msgs(), scope="contact")
        md = render_ai(out)
        self.assertIn("性格特质", md)
        self.assertIn("反例/替代解释", md)
        self.assertIn("不是心理诊断", md)


def _samples_as_msgs():
    """把 _samples() 还原成 profile 管道的 msgs 形态（ts 为 int 时间戳）。"""
    return [
        {"ts": 1742000000 + i * 3600, "kind": "text",
         "direction": "out" if s["uid"].startswith("m") else "in",
         "text": s["text"], "session": "测试会话"}
        for i, s in enumerate(_samples())
    ]


class TestProfileIntegration(unittest.TestCase):
    """profile.py 的 AI 挂载点：注入假 LLM 端到端。"""

    def setUp(self):
        from core import aiprofile
        aiprofile._cache().clear()

    def test_ai_layer_attaches(self):
        from core.profile import ProfileDistiller
        pd = ProfileDistiller(FakeDS(), ai="auto", llm=FakeLlm([json.dumps(GOOD_REPLY)]))
        d = pd._ai_layer(_profile(), _samples_as_msgs())
        self.assertTrue(d["available"])
        self.assertTrue(any(x["label"] == "性格特质" for x in d["dimensions"]))

    def test_ai_layer_off_returns_none(self):
        from core.profile import ProfileDistiller
        pd = ProfileDistiller(FakeDS(), ai="off")
        self.assertIsNone(pd._ai_layer(_profile(), _samples_as_msgs()))

    def test_ai_layer_force_overrides_off(self):
        """GUI 路径：锁内用 ai="off" 跑统计，锁外 force=True 外挂 AI 章节。

        没有 force 时第一道守卫会把自己想要的调用挡掉——真机联调实测过
        GUI 按钮点了不生成（result.ai 恒为 None）。
        """
        from core.profile import ProfileDistiller
        pd = ProfileDistiller(FakeDS(), ai="off",
                              llm=FakeLlm([json.dumps(GOOD_REPLY)]))
        d = pd._ai_layer(_profile(), _samples_as_msgs(), force=True)
        self.assertTrue(d["available"])
        self.assertTrue(any(x["label"] == "性格特质" for x in d["dimensions"]))

    def test_ai_layer_degrades_without_llm(self):
        import core.bot.llm  # noqa: F401  —— 先让 .env 加载完再摘 key
        from core.profile import ProfileDistiller
        from unittest.mock import patch
        for k in ("MOSU_BOT_API_KEY", "MOSU_BOT_BASE_URL", "MOSU_BOT_MODEL"):
            os.environ.pop(k, None)
        with patch("core.llmconfig.load_llm_config",
                   return_value={"baseUrl": "", "apiKey": "", "model": "",
                                 "temperature": 0.8}):
            pd = ProfileDistiller(FakeDS(), ai="auto")
            self.assertIsNone(pd._ai_layer(_profile(), _samples_as_msgs()))

    def test_ai_layer_exception_degrades(self):
        """AI 层任何异常都不能炸掉画像本身。"""
        from core.profile import ProfileDistiller

        class BoomLlm:
            model = "boom"

            def chat(self, *a, **kw):
                raise RuntimeError("网络炸了")

        pd = ProfileDistiller(FakeDS(), ai="auto", llm=BoomLlm())
        d = pd._ai_layer(_profile(), _samples_as_msgs())
        self.assertFalse(d["available"])
        self.assertIn("RuntimeError", d["reason"])


class TestPromptBuild(unittest.TestCase):
    def test_prompt_contains_uids_and_facts(self):
        from core.aiprofile import build_messages
        msgs = build_messages(_profile(), _samples(), "contact",
                              subject="张三", self_name="白墨")
        user = msgs[1]["content"]
        self.assertIn("m0", user)
        self.assertIn("t1", user)
        self.assertIn("张三", user)
        self.assertIn("统计事实", user)
        self.assertIn("不得引入外部猜测", msgs[0]["content"])

    def test_directions_use_real_names(self):
        """方向必须实名标注（我/对方 会被推理模型误读成分析对象）。"""
        from core.aiprofile import build_messages
        msgs = build_messages(_profile(), _samples(), "contact",
                              subject="张三", self_name="白墨")
        user = msgs[1]["content"]
        self.assertIn("白墨", user)
        self.assertIn("方向语义", user)
        self.assertIn("必须取自张三方向的消息", user)
        # m* 样本旁必须是本人名，不能出现裸「我」
        line_m = next(l for l in user.splitlines() if l.startswith("m0 "))
        self.assertIn("白墨", line_m)
        line_t = next(l for l in user.splitlines() if l.startswith("t0 "))
        self.assertIn("张三", line_t)


if __name__ == "__main__":
    unittest.main()
