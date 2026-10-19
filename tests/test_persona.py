# -*- coding: utf-8 -*-
"""四种 AI 角色：定义、切换指令、prompt 组装与缓存隔离。"""
import tempfile
import unittest
from pathlib import Path
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

    def test_default_is_listener(self):
        """默认必须是倾听者：logo/文案把墨溯定位成陪伴助手，
        第一句话却是模仿者＝产品承诺与实际行为不一致。"""
        from core.bot.personas import DEFAULT_PERSONA, PERSONAS
        self.assertEqual(DEFAULT_PERSONA, "listener")
        self.assertIn(DEFAULT_PERSONA, PERSONAS)

    def test_listener_ships_first(self):
        """列表顺序＝控制台卡片顺序，listener 必须排在最前。"""
        from core.bot.personas import PERSONAS
        self.assertEqual(list(PERSONAS)[0], "listener")


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

    def test_help_says_default_listener(self):
        """帮助文案里的"默认 X"必须跟 DEFAULT_PERSONA 一致，
        否则用户按提示切人格却发现默认不是那个。"""
        from core.bot.personas import parse_switch, SWITCH_HELP, DEFAULT_PERSONA, get
        self.assertIn("默认倾听者", SWITCH_HELP)
        self.assertNotIn("默认模仿", SWITCH_HELP)
        self.assertIn(get(DEFAULT_PERSONA).name, SWITCH_HELP)
        # 切换指令本身仍要覆盖全部四个人格
        pid, _ = parse_switch("/倾听")
        self.assertEqual(pid, DEFAULT_PERSONA)


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


class TestContextBucketing(unittest.TestCase):
    """上下文按人格分桶：同一 conv_id 下四个人格互不可见。

    分桶而非过滤是产品决策：过滤会让「切人格后 AI 忘了刚才说的话」，
    分桶既保住同一人格内的连续性，又杜绝跨人格串味。
    """

    def _svc(self, tmp):
        """落盘 store 指向临时目录。

        用真实 ConversationStore（含真实 _fname 文件名逻辑），只把落盘目录
        换到 tmp——改 MOSU_HOME 是全局副作用，会污染同进程其他测试。
        """
        from core.bot.store import ConversationStore
        import core.bot.store as ST
        from unittest.mock import patch
        svc = _svc()
        svc._store_patch = patch.object(ST, "dir_for", lambda: Path(tmp))
        svc._store_patch.start()
        self.addCleanup(svc._store_patch.stop)
        svc.store = ConversationStore(limit=svc.history_limit)
        return svc

    def test_bucket_id_format(self):
        from core.bot.service import bucket_id
        self.assertEqual(bucket_id("x", "listener"), "x::listener")
        self.assertEqual(bucket_id(None, "mimic"), "default::mimic")
        self.assertEqual(bucket_id("", "analyst"), "default::analyst")

    def test_two_personas_do_not_see_each_other(self):
        svc = self._svc(tempfile.mkdtemp(prefix="mosu-bucket-"))
        svc.llm.chat.return_value = "嗯"
        svc.chat("我今天有点累", conv_id="x", persona="listener")
        svc.llm.chat.return_value = "平均回复时延 4 分钟"
        svc.chat("我今天有点累", conv_id="x", persona="mimic")

        self.assertEqual(len(svc._history("x::listener")), 2)
        self.assertEqual(len(svc._history("x::mimic")), 2)
        # 互不可见：倾听者桶里绝不能出现模仿者的回答
        lis = [h["content"] for h in svc._history("x::listener")]
        mim = [h["content"] for h in svc._history("x::mimic")]
        self.assertNotIn("平均回复时延 4 分钟", lis)
        self.assertNotIn("嗯", mim)
        # 原始 conv_id 不再是一个共享桶
        self.assertEqual(len(svc._history("x")), 0)

    def test_disk_has_two_bucketed_files(self):
        """落盘文件名按 cid 分桶——两个桶必须是两个文件，否则重启后串味。"""
        from core.bot.store import _fname
        tmp = tempfile.mkdtemp(prefix="mosu-bucket-")
        svc = self._svc(tmp)
        svc.llm.chat.return_value = "嗯"
        svc.chat("在吗", conv_id="wxid_a", persona="listener")
        svc.chat("在吗", conv_id="wxid_a", persona="mimic")

        files = sorted(p.name for p in Path(tmp).glob("*.json"))
        self.assertEqual(len(files), 2, f"应有两个桶文件，实际 {files}")
        self.assertIn(_fname("wxid_a::listener"), files)
        self.assertIn(_fname("wxid_a::mimic"), files)

    def test_history_prompt_only_has_own_bucket(self):
        """发给 LLM 的历史里不能混进别的人格——这是串味的实际发生点。"""
        svc = self._svc(tempfile.mkdtemp(prefix="mosu-bucket-"))
        svc.llm.chat.return_value = "嗯"
        svc.chat("第一句", conv_id="y", persona="listener")
        svc.llm.chat.return_value = "我是模仿者"
        svc.chat("第二句", conv_id="y", persona="mimic")

        svc.llm.chat.reset_mock()
        svc.llm.chat.return_value = "在的"
        svc.chat("第三句", conv_id="y", persona="listener")
        sent = svc.llm.chat.call_args[0][0]
        blob = " ".join(m["content"] for m in sent)
        self.assertIn("第一句", blob)
        self.assertNotIn("我是模仿者", blob)

    def test_chat_passes_same_cid_downstream(self):
        """chat() 里 cid 只算一次；下游重拼会把桶拆开。"""
        import inspect
        from core.bot.service import BotService
        src = inspect.getsource(BotService.chat)
        self.assertIn("cid = bucket_id(conv_id, p.id)", src)
        # _history / _record_history 收到的必须都是这个 cid 变量，不能是 conv_id
        self.assertIn("self._record_history(cid,", src)
        self.assertIn("self._build_messages(query, retrieved, cid,", src)
        self.assertNotIn("self._record_history(conv_id,", src)


class TestWechatPersonaIntro(unittest.TestCase):
    """微信首次回复后的风格告知：进程内只发一次，且只在默认人格下触发。"""

    def setUp(self):
        import core.wechat as W
        self.W = W
        self._old = W._greeted
        W._greeted = False
        self.addCleanup(lambda: setattr(W, "_greeted", self._old))

    def test_intro_names_default_persona(self):
        from core.bot.personas import DEFAULT_PERSONA, get
        self.assertIn(f"「{get(DEFAULT_PERSONA).name}」", self.W.PERSONA_INTRO)

    def test_intro_lists_all_switch_commands(self):
        """告知里必须把三种可换风格都写出来，否则用户不知道能换。"""
        for cmd in ("/模仿", "/分析", "/安慰"):
            self.assertIn(cmd, self.W.PERSONA_INTRO)

    def test_intro_explains_each_persona(self):
        for hint in ("你的口气", "真实数据", "接情绪"):
            self.assertIn(hint, self.W.PERSONA_INTRO)

    def test_latch_starts_false(self):
        self.assertFalse(self.W._greeted)

    def test_greeting_guarded_by_latch_in_source(self):
        """真跑 run_serve 要连长轮询，太重；这里锁住门闩的三条约束。"""
        import inspect
        src = inspect.getsource(self.W.run_serve)
        self.assertIn("global _greeted", src)
        self.assertIn("if not _greeted and", src)
        self.assertIn("_greeted = True", src)
        # 门闩置位必须在 send_text 之后：发失败不该白白消耗掉唯一一次机会
        i_latch = src.index("if not _greeted and")
        block = src[i_latch:i_latch + 400]
        self.assertLess(block.index("send_text"), block.index("_greeted = True"))

    def test_greeting_failure_does_not_break_main_flow(self):
        """介绍发不出去只是少一句话，不能连带把正常回复判为失败。"""
        import inspect
        src = inspect.getsource(self.W.run_serve)
        i = src.index("if not _greeted and")
        block = src[i:src.index("_write_json(ctx_path(), ctx)", i)]
        self.assertIn("except Exception", block)

    def test_greeting_only_for_default_persona(self):
        """切到别的风格就不该再自我介绍——那时说"我是倾听者"是撒谎。"""
        from core.bot.personas import DEFAULT_PERSONA
        for pid in ("analyst", "comforter", "mimic"):
            self.assertNotEqual(pid, DEFAULT_PERSONA)

    def test_switch_command_uses_continue_not_greeting(self):
        """切换指令走 continue，不经过发消息那段——不该触发门闩。"""
        import inspect
        src = inspect.getsource(self.W.run_serve)
        i = src.index("if pid:")
        block = src[i:src.index("# 先给", i)]
        self.assertIn("continue", block)
        self.assertNotIn("_greeted", block)


if __name__ == "__main__":
    unittest.main()