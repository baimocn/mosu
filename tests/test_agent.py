# -*- coding: utf-8 -*-
"""Agent 引擎与工具层的离线测试（不调 LLM、不碰真实库）。

覆盖：
  - parse_when 的时间换算（这是最容易出错、错了整个答案就废的部分）
  - 工具 schema 的形状（OpenAI function calling 格式）
  - ToolBox 的分发与失败兜底
  - 提示词的关键约束是否在位（防幻觉条款不能被改掉）
  - LlmClient._parse_tool_resp 的解析（工具调用时 content 可能是 null）
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agent_engine import SYSTEM, InsightAgent, _tool_label, _tool_payload
from core.agent_tools import ToolBox, parse_when


class TestParseWhen(unittest.TestCase):
    """时间换算：模型自己换算不可靠，这里必须一次算对。"""

    def _fmt(self, ts):
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else None

    def test_ym_start_is_first_of_month(self):
        self.assertEqual(self._fmt(parse_when("2026-09")), "2026-09-01 00:00")

    def test_ym_end_is_last_of_same_month(self):
        # 曾经写成 mk(y, mo, 1) - 1，算出来是**上个月**末 —— 整个区间错位一个月
        self.assertEqual(self._fmt(parse_when("2026-09", end=True)), "2026-09-30 23:59")

    def test_december_end_rolls_over(self):
        self.assertEqual(self._fmt(parse_when("2025-12", end=True)), "2025-12-31 23:59")

    def test_chinese_year_month(self):
        self.assertEqual(self._fmt(parse_when("2026年3月")), "2026-03-01 00:00")
        self.assertEqual(self._fmt(parse_when("2026年3月", end=True)), "2026-03-31 23:59")

    def test_end_never_before_start(self):
        for p in ["上个月", "2026-09", "2026-10", "2025-08", "最近30天", "去年",
                  "今年", "这个月", "上周", "昨天", "2025-08-15", "2026年3月"]:
            a, b = parse_when(p), parse_when(p, end=True)
            self.assertIsNotNone(a, p)
            self.assertIsNotNone(b, p)
            self.assertGreaterEqual(b, a, f"{p}: end 早于 start")

    def test_relative_month(self):
        now = datetime.now()
        y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        self.assertEqual(self._fmt(parse_when("上个月"))[:7], f"{y:04d}-{m:02d}")

    def test_empty_returns_none(self):
        self.assertIsNone(parse_when(""))
        self.assertIsNone(parse_when(None))
        self.assertIsNone(parse_when("   "))


class TestToolSchemas(unittest.TestCase):
    """工具 schema 必须是合法的 OpenAI function calling 形状。"""

    def setUp(self):
        self.schemas = ToolBox.schemas()

    def test_not_empty(self):
        self.assertGreaterEqual(len(self.schemas), 8)

    def test_shape(self):
        for s in self.schemas:
            self.assertEqual(s["type"], "function")
            fn = s["function"]
            self.assertIn("name", fn)
            self.assertIn("description", fn)
            self.assertIn("parameters", fn)
            self.assertEqual(fn["parameters"]["type"], "object")
            self.assertIn("properties", fn["parameters"])

    def test_required_fields_exist_in_properties(self):
        for s in self.schemas:
            props = s["function"]["parameters"].get("properties", {})
            for req in s["function"]["parameters"].get("required", []):
                self.assertIn(req, props, f"{s['function']['name']}: required 字段 {req} 未声明")

    def test_names_unique(self):
        names = [s["function"]["name"] for s in self.schemas]
        self.assertEqual(len(names), len(set(names)))

    def test_json_serializable(self):
        json.dumps(self.schemas, ensure_ascii=False)


class TestToolBoxDispatch(unittest.TestCase):
    """分发与失败兜底。"""

    class _FakeDs:
        def sessions(self):
            return []

        def find_session(self, q):
            return None

    def setUp(self):
        self.box = ToolBox(self._FakeDs())

    def test_unknown_tool_returns_readable_error(self):
        r = self.box.run("no_such_tool", {})
        self.assertIn("没有工具", r["summary"])
        self.assertEqual(r["evidence"], [])

    def test_tool_exception_becomes_summary(self):
        class Boom:
            def sessions(self):
                raise RuntimeError("炸了")

        box = ToolBox(Boom())
        r = box.run("list_sessions", {})
        self.assertIn("执行失败", r["summary"])
        self.assertIn("炸了", r["summary"])

    def test_run_always_has_standard_keys(self):
        r = self.box.run("list_sessions", {})
        for k in ("summary", "data", "evidence", "tool", "args", "cost"):
            self.assertIn(k, r)

    def test_resolve_time_tool(self):
        r = self.box.run("resolve_time", {"phrase": "2026-09"})
        self.assertIn("2026-09-01", r["summary"])
        r2 = self.box.run("resolve_time", {"phrase": "2026-09", "end": True})
        self.assertIn("2026-09-30", r2["summary"])

    def test_resolve_time_unknown_phrase(self):
        r = self.box.run("resolve_time", {"phrase": "瞎写的"})
        self.assertIn("没听懂", r["summary"])


class TestPromptContract(unittest.TestCase):
    """提示词的关键约束不能被改掉——它们是防幻觉的底线。"""

    def test_has_no_self_arithmetic_rule(self):
        self.assertIn("数字必须来自工具返回", SYSTEM)

    def test_has_no_fabrication_rule(self):
        self.assertIn("一条消息都不能编", SYSTEM)

    def test_has_evidence_requirement(self):
        self.assertIn("依据", SYSTEM)

    def test_has_time_anchor(self):
        # 模型不知道今天几号就会瞎猜月份，必须注入当前时间
        self.assertIn("{now}", SYSTEM)
        self.assertIn("{weekday}", SYSTEM)

    def test_mentions_resolve_time(self):
        self.assertIn("resolve_time", SYSTEM)

    def test_has_no_sql_escape_hatch(self):
        # 墨溯刻意不给模型写 SQL 的能力（竞品给了 query_sql 兜底）——
        # 给了就有编数字的口子
        self.assertNotIn("query_sql", SYSTEM)


class TestToolHelpers(unittest.TestCase):
    def test_tool_label_readable(self):
        self.assertIn("搜消息", _tool_label("search_messages", {"keyword": "考试"}))
        self.assertIn("换算时间", _tool_label("resolve_time", {"phrase": "上个月"}))

    def test_tool_payload_truncates(self):
        big = {"summary": "x" * 100, "data": {"k": ["y" * 500] * 50}}
        p = _tool_payload(big)
        self.assertLessEqual(len(p), 4200)
        self.assertIn("截断", p)

    def test_tool_payload_small_untouched(self):
        p = _tool_payload({"summary": "ok", "data": {"a": 1}})
        self.assertIn("ok", p)


class TestToolCallParsing(unittest.TestCase):
    """LLM 返回的 tool_calls 解析：content 可能是 null。"""

    def test_parse_with_tool_calls(self):
        from core.bot.llm import LlmClient
        data = {"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "chat_stats", "arguments": '{"metric":"overview"}'}}]}}],
            "usage": {"total_tokens": 100}}
        r = LlmClient._parse_tool_resp(data)
        self.assertEqual(r["content"], "")
        self.assertEqual(len(r["tool_calls"]), 1)
        self.assertEqual(r["tool_calls"][0]["name"], "chat_stats")
        self.assertEqual(r["tool_calls"][0]["args"]["metric"], "overview")

    def test_parse_broken_arguments(self):
        from core.bot.llm import LlmClient
        data = {"choices": [{"message": {"content": "hi", "tool_calls": [
            {"id": "c", "function": {"name": "x", "arguments": "not json"}}]}}]}
        r = LlmClient._parse_tool_resp(data)
        self.assertEqual(r["content"], "hi")
        self.assertIn("_raw", r["tool_calls"][0]["args"])

    def test_parse_no_tools(self):
        from core.bot.llm import LlmClient
        data = {"choices": [{"message": {"content": "答案"}}]}
        r = LlmClient._parse_tool_resp(data)
        self.assertEqual(r["content"], "答案")
        self.assertEqual(r["tool_calls"], [])


if __name__ == "__main__":
    unittest.main()


class TestNumberAudit(unittest.TestCase):
    """答案里的数字必须有工具结果做出处。

    背景：实测模型会把工具返回的 2814 条自己改口成 2904 条——
    用户按 2904 去核对，怎么都对不上。这是程序化兜底。
    """

    def _run(self, answer, tool_nums):
        from core.agent_engine import AgentRun
        r = AgentRun("q")
        r.answer = answer
        r.tool_numbers = set(tool_nums)
        return r

    def test_number_with_source_passes(self):
        from core.agent_engine import _audit_numbers
        r = self._run("他一共发了 2814 条消息。", ["2814", "120"])
        out = _audit_numbers(r)
        self.assertNotIn("核对提醒", out)

    def test_number_without_source_is_flagged(self):
        from core.agent_engine import _audit_numbers
        r = self._run("他一共发了 2904 条消息。", ["2814", "120"])
        out = _audit_numbers(r)
        self.assertIn("核对提醒", out)
        self.assertIn("2904", out)

    def test_thousand_separator_matches(self):
        """答案写 2,904，工具返回 2904 —— 同一个数，不该报警。"""
        from core.agent_engine import _audit_numbers
        r = self._run("共 2,904 条。", ["2904"])
        self.assertNotIn("核对提醒", _audit_numbers(r))

    def test_percentage_matches_source(self):
        """答案写 90%，工具返回 90 —— 不该报警。"""
        from core.agent_engine import _audit_numbers
        r = self._run("我方占比 90%。", ["90", "200"])
        self.assertNotIn("核对提醒", _audit_numbers(r))

    def test_structural_numbers_allowed(self):
        """年份、月份、小数字不是统计量，不该报警。"""
        from core.agent_engine import _audit_numbers
        r = self._run("2026 年 10 月的数据里，12 条消息。", ["2814"])
        out = _audit_numbers(r)
        # 10 / 12 / 2026 属豁免，不该出现提醒
        self.assertNotIn("核对提醒", out)

    def test_empty_answer_untouched(self):
        from core.agent_engine import _audit_numbers
        r = self._run("", ["1"])
        self.assertEqual(_audit_numbers(r), "")

    def test_audit_does_not_mutate_answer_body(self):
        """审计只追加提醒，不改原文。"""
        from core.agent_engine import _audit_numbers
        r = self._run("结论是 2904 条。", ["2814"])
        out = _audit_numbers(r)
        self.assertTrue(out.startswith("结论是 2904 条。"))


class TestStructuralNumberExemption(unittest.TestCase):
    """日期/时刻里的数字不是统计量，不该被当成「模型自己算数」。

    实测踩过：答案写「10 月 27 日凌晨 3 点」，审计把 27 报成无出处。
    """

    def _audit(self, ans, srcs):
        from core.agent_engine import AgentRun, _audit_numbers
        r = AgentRun("q")
        r.answer = ans
        r.tool_numbers = set(srcs)
        return _audit_numbers(r)

    def test_date_and_hour_exempt(self):
        out = self._audit("2026 年 10 月 27 日那天聊到凌晨 3 点。", {"2814"})
        self.assertNotIn("核对提醒", out)

    def test_iso_date_exempt(self):
        out = self._audit("记录发生在 2026-10-27。", {"2814"})
        self.assertNotIn("核对提醒", out)

    def test_message_count_still_flagged(self):
        """「12 条消息」是统计量，仍要查（12 在白名单，但 37 不在）。"""
        out = self._audit("他一共发了 37 条消息。", {"2814"})
        self.assertIn("核对提醒", out)
        self.assertIn("37", out)


class TestToolPayloadTruncation(unittest.TestCase):
    """回喂被截断时，必须明说「字段缺失别自己填 0」。

    实测踩过：my_profile 的 data 接近 4000 字符上限，截断后
    overview.myShare 被切掉，模型就报「myMessages: 0、myShare: 0」
    （真实值 19719 / 0.081）。
    """

    def test_short_payload_not_truncated(self):
        from core.agent_engine import _tool_payload_safe
        r = {"summary": "ok", "data": {"a": 1}}
        out = _tool_payload_safe(r)
        self.assertNotIn("不完整", out)

    def test_long_payload_marks_incomplete(self):
        from core.agent_engine import _tool_payload_safe
        r = {"summary": "ok", "data": {"blob": "x" * 9000}}
        out = _tool_payload_safe(r)
        self.assertIn("不完整", out)
        self.assertIn("不要自己填", out)
        self.assertLess(len(out), 4000)

    def test_my_profile_payload_fits(self):
        """my_profile 的回喂不能超限（超了就会截断掉关键字段）。

        不依赖真实微信库：把 ProfileDistiller 换成一个返回「超大画像」的假实现，
        走**真实**的 t_my_profile 代码路径，验证它吐给模型的是精简版。
        """
        import core.profile as profile_mod
        from core.agent_engine import _tool_payload_safe
        from core.agent_tools import ToolBox

        big = {
            "scope": "self",
            "meta": {"scannedMessages": 243616},
            "overview": {"totalMessages": 243616, "myMessages": 19719, "myShare": 0.081},
            "rhythm": {"hourly": [{"hour": h, "total": 1, "mine": 1} for h in range(24)],
                       "nightMessages": 1577},
            "style": {"avgLength": 12.6, "byKind": {"text": 15035}},
            "topics": [{"topic": "x" * 50, "hits": 100, "ratio": 0.1} for _ in range(60)],
            "monthly": [{"month": f"2026-{i:02d}", "total": 1000} for i in range(1, 13)],
            "insights": ["短句派：本人平均消息 12.6 字，沟通直接"],
        }
        self.assertGreater(len(str(big)), 4000, "假画像必须够大才能验证裁剪")

        class FakeDistiller:
            def __init__(self, ds, *a, **kw):
                pass

            def self_profile(self, from_ts=None, to_ts=None):
                return big

        orig = profile_mod.ProfileDistiller
        try:
            profile_mod.ProfileDistiller = FakeDistiller
            tb = ToolBox.__new__(ToolBox)      # 不碰 Dataset，避免依赖真实数据
            tb.ds = None
            tb._sessions_cache = None
            r = tb.run("my_profile", {})
        finally:
            profile_mod.ProfileDistiller = orig

        out = _tool_payload_safe(r)
        self.assertLess(len(out), 4000, "my_profile 回喂超限 → 会被截断 → 模型瞎填 0")
        self.assertIn("myMessages", out)
        self.assertIn("myShare", out)
        # 冗余大块不该进回喂
        self.assertNotIn("monthly", out)
        self.assertNotIn("topics", out)


class TestParseWhenMonthOnly(unittest.TestCase):
    """只说月份不给年份：「10月」「10月份」。

    实测踩过：问「10 月发了多少条」，resolve_time 报「没听懂」，
    模型只好自己猜，猜成了去年 10 月 —— 整条答案全错，
    而且答案看起来完全合理，用户根本发现不了。
    """

    def _fmt(self, ts):
        import datetime
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def test_month_with_suffix(self):
        from core.agent_tools import parse_when
        self.assertIn("-10-01 00:00:00", self._fmt(parse_when("10月份")))
        self.assertIn("-10-31 23:59:59", self._fmt(parse_when("10月份", end=True)))

    def test_month_without_suffix(self):
        from core.agent_tools import parse_when
        self.assertIn("-10-01 00:00:00", self._fmt(parse_when("10月")))

    def test_future_month_rolls_to_last_year(self):
        """今年还没到的月份，取去年那个 —— 数据里不存在未来的月份。"""
        import datetime
        from core.agent_tools import parse_when
        now = datetime.datetime.now()
        future = now.month % 12 + 1      # 下个月（今年还没到）
        ts = parse_when(f"{future}月")
        self.assertEqual(datetime.datetime.fromtimestamp(ts).year, now.year - 1,
                         f"「{future}月」应取去年，实际取到今年")

    def test_current_or_past_month_uses_this_year(self):
        import datetime
        from core.agent_tools import parse_when
        now = datetime.datetime.now()
        ts = parse_when(f"{now.month}月")
        self.assertEqual(datetime.datetime.fromtimestamp(ts).year, now.year)

    def test_month_end_is_last_day(self):
        """月末必须是当月最后一天，不能串到下月。"""
        import datetime
        from core.agent_tools import parse_when
        dt = datetime.datetime.fromtimestamp(parse_when("10月", end=True))
        self.assertEqual((dt.month, dt.day), (10, 31))
        self.assertEqual((dt.hour, dt.minute, dt.second), (23, 59, 59))
