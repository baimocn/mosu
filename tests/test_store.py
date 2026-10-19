# -*- coding: utf-8 -*-
"""对话上下文持久化：落盘、截断、重启恢复、失败降级。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("MOSU_HOME")
        os.environ["MOSU_HOME"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MOSU_HOME", None)
        else:
            os.environ["MOSU_HOME"] = self._old


class TestFileNaming(_Env):
    def test_unsafe_chars_sanitized(self):
        from core.bot.store import _fname
        f = _fname("wxid_abc@chatroom/../etc")
        self.assertNotIn("/", f)
        self.assertNotIn("@", f)
        self.assertNotIn("..", f)
        self.assertTrue(f.endswith(".json"))

    def test_different_ids_different_files(self):
        from core.bot.store import _fname
        self.assertNotEqual(_fname("wxid_a1"), _fname("wxid_a12"))

    def test_empty_id_becomes_default(self):
        from core.bot.store import _fname
        self.assertTrue(_fname("").startswith("default-"))


class TestConversationStore(_Env):
    def _turn(self, q, a):
        return [{"role": "user", "content": q}, {"role": "assistant", "content": a}]

    def test_append_and_load(self):
        from core.bot.store import ConversationStore
        s = ConversationStore(limit=5)
        s.append("c1", self._turn("在吗", "在"))
        got = s.load("c1")
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["content"], "在吗")

    def test_isolation_between_conversations(self):
        from core.bot.store import ConversationStore
        s = ConversationStore(limit=5)
        s.append("c1", self._turn("a", "A"))
        s.append("c2", self._turn("b", "B"))
        self.assertEqual(len(s.load("c1")), 2)
        self.assertEqual(s.load("c1")[0]["content"], "a")
        self.assertEqual(s.load("c2")[0]["content"], "b")

    def test_truncates_to_limit(self):
        """每会话只留最近 N 轮，文件不能无界增长。"""
        from core.bot.store import ConversationStore
        s = ConversationStore(limit=2)
        for i in range(6):
            s.append("c1", self._turn(f"q{i}", f"a{i}"))
        got = s.load("c1")
        self.assertEqual(len(got), 4)          # 2 轮 × 2 条
        self.assertEqual(got[-1]["content"], "a5")

    def test_load_missing_returns_empty(self):
        from core.bot.store import ConversationStore
        self.assertEqual(ConversationStore().load("不存在"), [])

    def test_corrupt_file_degrades(self):
        from core.bot.store import ConversationStore, dir_for, _fname
        d = dir_for()
        d.mkdir(parents=True, exist_ok=True)
        (d / _fname("c1")).write_text("{ 这不是 json", encoding="utf-8")
        s = ConversationStore()
        self.assertEqual(s.load("c1"), [])
        self.assertTrue(s._failed)             # 一次性降级，不反复重试

    def test_env_switch_disables(self):
        from core.bot.store import ConversationStore
        os.environ["MOSU_BOT_HISTORY"] = "0"
        try:
            s = ConversationStore(limit=5)
            s.append("c1", self._turn("q", "a"))
            self.assertEqual(s.load("c1"), [])
        finally:
            os.environ.pop("MOSU_BOT_HISTORY", None)

    def test_clear(self):
        from core.bot.store import ConversationStore
        s = ConversationStore(limit=5)
        s.append("c1", self._turn("q", "a"))
        s.append("c2", self._turn("q", "a"))
        self.assertEqual(s.clear("c1"), 1)
        self.assertEqual(s.load("c1"), [])
        self.assertEqual(len(s.load("c2")), 2)

    def test_clear_all(self):
        from core.bot.store import ConversationStore
        s = ConversationStore(limit=5)
        s.append("c1", self._turn("q", "a"))
        s.append("c2", self._turn("q", "a"))
        self.assertEqual(s.clear(), 2)


def _svc(history_limit=20):
    from core.bot.service import BotService
    r = MagicMock()
    r.corpus = [{"text": "x"}]
    r.search.return_value = [{"text": "y", "session": "s"}]
    ck = MagicMock()
    ck.check.return_value = (True, [])
    svc = BotService("c.jsonl", retriever=r, checker=ck, history_limit=history_limit)
    svc.llm = MagicMock()
    svc.llm.chat.return_value = "好"
    return svc


class TestHistoryIntegration(_Env):
    def test_survives_restart(self):
        """常驻进程重启后，新实例应能读到旧对话（这是落盘的目的）。"""
        a = _svc()
        a.chat("在吗", conv_id="wxid_x")
        b = _svc()                      # 模拟重启
        msgs = b.preview_prompt("继续", conv_id="wxid_x")["messages"]
        user_turns = [m["content"] for m in msgs if m["role"] == "user"]
        self.assertIn("在吗", user_turns)

    def test_no_double_load(self):
        """同一实例内同一会话只能从磁盘载入一次，否则上下文会重复。"""
        s = _svc()
        s.chat("第一句", conv_id="c")
        s.chat("第二句", conv_id="c")
        self.assertEqual(len(s.history["c::listener"]), 4)

    def test_memory_history_still_works_without_store(self):
        from core.bot.service import BotService
        r = MagicMock()
        r.corpus = [{"text": "x"}]
        r.search.return_value = []
        ck = MagicMock()
        ck.check.return_value = (True, [])
        svc = BotService("c.jsonl", retriever=r, checker=ck, store=None)
        svc.llm = MagicMock()
        svc.llm.chat.return_value = "好"
        svc.chat("q", conv_id="c")
        self.assertEqual(len(svc.history["c::listener"]), 2)

    def test_restart_does_not_leak_across_personas(self):
        """重启后换人格，读到的必须是那个人格自己的桶（不是别人的历史）。"""
        a = _svc()
        a.chat("我今天有点累", conv_id="wxid_y", persona="listener")
        b = _svc()
        msgs = b.preview_prompt("继续", conv_id="wxid_y", persona="mimic")["messages"]
        self.assertNotIn("我今天有点累", [m["content"] for m in msgs])


if __name__ == "__main__":
    unittest.main()