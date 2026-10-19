# -*- coding: utf-8 -*-
"""四种 AI 角色：定义、切换指令、prompt 组装与缓存隔离。"""
import unittest
from unittest.mock import MagicMock


class TestPersonaRegistry(unittest.TestCase):
    def test_four_personas(self):
        from core.bot.personas import PERSONAS
        self.assertEqual(set(PERSONAS), {"listener", "analyst", "comforter", "mimic"})

    def test_each_has_essentials(self):
        from core.bot.personas import PERSONAS
        for p in PERSONAS.values():
            self.assertTrue(p.name and p.tagline and p.intent)
            self.assertTrue(p.persona_prompt.strip())
            self.assertTrue(p.guard.strip())
            self.assertIn(p.data, ("none", "light", "full"))
            self.assertGreater(p.max_words, 0)

    def test_only_data_personas_see_data(self):
        """倾听者/模仿者刻意看不到数据——立刻给结论正是倾听不需要的。"""
        from core.bot.personas import PERSONAS
        self.assertEqual(PERSONAS["listener"].data, "none")
        self.assertEqual(PERSONAS["mimic"].data, "none")
        self.assertEqual(PERSONAS["analyst"].data, "full")
        self.assertEqual(PERSONAS["comforter"].data, "light")

    def test_listener_shorter_than_analyst(self):
        from core.bot.personas import PERSONAS
        self.assertLess(PERSONAS["listener"].max_words, PERSONAS["analyst"].max_words)

    def test_get_falls_back(self):
        from core.bot.personas import get, DEFAULT_PERSONA
        self.assertEqual(get("不存在").id, DEFAULT_PERSONA)
        self.assertEqual(get(None).id, DEFAULT_PERSONA)
        self.assertEqual(get("analyst").id, "analyst")

    def test_list_personas_serializable(self):
        from core.bot.personas import list_personas
        items = list_personas()
        self.assertEqual(len(items), 4)
        for it in items:
            self.assertEqual(set(it), {"id", "name", "tagline", "intent", "data"})


class TestSwitchCommand(unittest.TestCase):
    def test_slash_and_plain(self):
        from core.bot.personas import parse_switch
        self.assertEqual(parse_switch("/倾听")[0], "listener")
        self.assertEqual(parse_switch("分析")[0], "analyst")
        self.assertEqual(parse_switch("安慰者")[0], "comforter")
        self.assertEqual(parse_switch("/mimic")[0], "mimic")

    def test_normal_text_untouched(self):
        from core.bot.personas import parse_switch
        for t in ("今天天气怎么样", "帮我看下这个", "", "分析一下这段代码"):
            pid, rest = parse_switch(t)
            self.assertIsNone(pid)
            self.assertEqual(rest, t)

    def test_help(self):
        from core.bot.personas import parse_switch, SWITCH_HELP
        pid, _ = parse_switch("/help")
        self.assertEqual(pid, "__help__")
        self.assertIn("倾听", SWITCH_HELP)


def _svc(retrieved=None):
    """构造一个不碰 LLM 的 BotService（llm/checker 都打桩）。"""
    from core.bot.service import BotService
    retriever = MagicMock()
    retriever.corpus = [{"text": "样本"}]
    retriever.search.return_value = retrieved if retrieved is not None else [
        {"text": "样本句", "session": "s1"}]
    checker = MagicMock()
    checker.check.return_value = (True, [])      # 直接通过，避免重试循环
    checker.feedback.return_value = "改一下"
    svc = BotService("corpus.jsonl", retriever=retriever, checker=checker)
    svc.llm = MagicMock()
    return svc


class TestPromptAssembly(unittest.TestCase):
    def test_persona_prompt_injected(self):
        svc = _svc()
        msgs = svc.preview_prompt("你好", persona="analyst")["messages"]
        sys_msg = msgs[0]["content"]
        self.assertIn("分析者", sys_msg)
        self.assertIn("红线", sys_msg)

    def test_mimic_has_no_persona_header(self):
        """模仿者本身就是数字分身，重复追加角色段只会浪费 token。"""
        svc = _svc()
        msgs = svc.preview_prompt("你好", persona="mimic")["messages"]
        sys_msg = msgs[0]["content"]
        self.assertNotIn("你现在扮演的角色", sys_msg)
        # 但风格语料仍在
        self.assertTrue(any("参考片段" in m["content"] for m in msgs))

    def test_non_mimic_skips_corpus(self):
        """非模仿者不需要风格语料，避免干扰角色设定。"""
        svc = _svc()
        msgs = svc.preview_prompt("你好", persona="listener")["messages"]
        self.assertFalse(any("参考片段" in m["content"] for m in msgs))

    def test_facts_only_for_data_personas(self):
        svc = _svc()
        svc.ds = MagicMock()
        # 让事实层直接返回固定文本
        import core.bot.service as S
        svc._facts_for = lambda p, q: ("事实：共 100 条" if p.data != "none" else "")
        a = svc.preview_prompt("我们聊了多少", persona="analyst")["messages"]
        self.assertTrue(any("事实：共 100 条" in m["content"] for m in a))
        l = svc.preview_prompt("我们聊了多少", persona="listener")["messages"]
        self.assertFalse(any("事实：共 100 条" in m["content"] for m in l))


class TestCacheIsolation(unittest.TestCase):
    def test_cache_key_includes_persona(self):
        from core.bot.service import _cache_key, _norm_key
        self.assertNotEqual(_cache_key("在吗", "analyst"), _cache_key("在吗", "listener"))
        # 同一人格下等价问法仍命中
        self.assertEqual(_cache_key("在吗？", "analyst"), _cache_key("在 吗", "analyst"))
        self.assertEqual(_norm_key("在吗？"), "在吗")

    def test_chat_isolated_by_persona(self):
        svc = _svc()
        svc.llm.chat.return_value = "好"
        svc.chat("在吗", persona="analyst")
        svc.llm.chat.return_value = "嗯"
        r = svc.chat("在吗", persona="listener")
        self.assertEqual(r["reply"], "嗯")
        self.assertEqual(r["persona"], "listener")

    def test_set_persona_persists(self):
        svc = _svc()
        svc.set_persona("comforter")
        self.assertEqual(svc.persona_id, "comforter")
        svc.set_persona("乱填")
        self.assertEqual(svc.persona_id, "comforter")   # 未知 id 不改当前值


if __name__ == "__main__":
    unittest.main()