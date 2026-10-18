# -*- coding: utf-8 -*-
"""bot 测试：规则校验器 / 语料检索 / 编排重试（全部离线，LLM 用 mock）。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.bot import (  # noqa: E402
    BotService, StyleChecker, StyleRetriever,
)
from core.bot.checker import AI_CLICHES  # noqa: E402


def _write_corpus(td: Path, items) -> Path:
    p = td / "corpus.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for t, sess in items:
            f.write(json.dumps({"ts": 1770000000, "time": "2026-02-01 10:00",
                                "session": sess, "kind": "text", "text": t,
                                "emojis": [], "len": len(t)}, ensure_ascii=False) + "\n")
    return p


class TestChecker(unittest.TestCase):
    def setUp(self):
        self.c = StyleChecker()

    def test_ai_cliche_rejected(self):
        for w in ("很高兴为您解答", "综上所述，方案如下", "作为AI助手", "希望这对你有帮助"):
            ok, probs = self.c.check(w)
            self.assertFalse(ok, w)
            self.assertTrue(any("AI 腔" in p for p in probs))

    def test_too_long_rejected(self):
        ok, probs = self.c.check("字" * 100)
        self.assertFalse(ok)
        self.assertTrue(any("太长" in p for p in probs))

    def test_exclaim_rejected(self):
        ok, _ = self.c.check("太好了！！！真不错！！！")
        self.assertFalse(ok)

    def test_short_casual_passes(self):
        ok, probs = self.c.check("ok")
        self.assertTrue(ok, probs)
        ok2, _ = self.c.check("那明天再说[旺柴]")
        self.assertTrue(ok2)

    def test_copy_detection(self):
        src = {"text": "这个方案我觉得可以直接部署到服务器上试试"}
        ok, probs = self.c.check("这个方案我觉得可以直接部署到服务器上试试", [src])
        self.assertFalse(ok)
        self.assertTrue(any("照抄" in p for p in probs))

    def test_similar_but_reworded_passes(self):
        src = {"text": "这个方案我觉得可以直接部署到服务器上试试"}
        ok, _ = self.c.check("直接部署试试呗", [src])
        self.assertTrue(ok)

    def test_empty(self):
        ok, _ = self.c.check("   ")
        self.assertFalse(ok)

    def test_feedback_lists_problems(self):
        fb = StyleChecker.feedback(["太长（90 字）"])
        self.assertIn("太长", fb)
        self.assertIn("微信风格", fb)


class TestRetriever(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.corpus = _write_corpus(Path(self.tmp.name), [
            ("这个报价多少钱", "客户A"),
            ("价格好说，看需求", "客户A"),
            ("部署到服务器上就行了", "技术群"),
            ("AI 模型接口要这么调", "技术群"),
            ("晚上吃啥", "吃饭群"),
            ("食堂人超多", "吃饭群"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_basic_search(self):
        r = StyleRetriever(self.corpus)
        hits = r.search("部署")
        self.assertTrue(hits)
        self.assertIn("部署", hits[0]["text"])

    def test_synonym_expansion(self):
        """问「报价」应能命中写「多少钱/价格」的语料。"""
        r = StyleRetriever(self.corpus)
        hits = r.search("报价")
        texts = " ".join(h["text"] for h in hits)
        self.assertIn("多少钱", texts)

    def test_scene_boost(self):
        r = StyleRetriever(self.corpus)
        hits = r.search("服务器怎么部署代码")
        self.assertTrue(hits)
        self.assertIn("部署", hits[0]["text"])


class MockLlm:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, temperature=0.8, max_tokens=300):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else "ok"


class TestBotService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.corpus = _write_corpus(Path(self.tmp.name), [
            ("你去图书馆吗", "同学"),
            ("明天要早起", "同学"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def _bot(self, replies):
        bot = BotService(self.corpus)
        bot.llm = MockLlm(replies)
        return bot

    def test_pass_first_try(self):
        bot = self._bot(["走，早点去占位置"])
        r = bot.chat("明天要不要去图书馆")
        self.assertEqual(r["tries"], 1)
        self.assertTrue(r["passed"])
        self.assertEqual(r["reply"], "走，早点去占位置")

    def test_retry_after_ai_cliche(self):
        """第一次 AI 腔 → 自动带反馈重试 → 第二次通过。"""
        bot = self._bot(["很高兴为您服务！明天去图书馆是很棒的选择呢！", "去啊，早点去"])
        r = bot.chat("明天要不要去图书馆")
        self.assertEqual(r["tries"], 2)
        self.assertTrue(r["passed"])
        self.assertEqual(r["reply"], "去啊，早点去")
        # 重试时把反馈追加进了上下文
        last = bot.llm.calls[-1]
        self.assertTrue(any("不合格" in str(m.get("content", "")) for m in last))

    def test_gives_up_after_max_retries(self):
        bot = self._bot(["综上所述"] * 3)
        r = bot.chat("在吗")
        self.assertEqual(r["tries"], 3)          # 1 + max_retries(2)
        self.assertFalse(r["passed"])

    def test_history_recorded(self):
        bot = self._bot(["嗯"])
        bot.chat("在吗", conv_id="c1")
        bot.chat("还在吗", conv_id="c1")
        self.assertEqual(len(bot.history["c1"]), 4)   # 2 轮 × user+assistant

    def test_sample_mode(self):
        bot = self._bot(["嗯"])
        bot.sample_mode = True
        r = bot.chat("随便一句")
        self.assertTrue(r["retrieved"])
        self.assertEqual(r["tries"], 1)

    def test_prompt_structure(self):
        bot = self._bot([])
        pv = bot.preview_prompt("去图书馆吗")
        roles = [m["role"] for m in pv["messages"]]
        self.assertEqual(roles[0], "system")
        self.assertIn("user", roles)
        self.assertEqual(roles[-1], "user")
        # 参考样本消息必须包含"不要照抄"
        ref = next(m for m in pv["messages"] if m["role"] == "user")
        self.assertIn("不要照抄", ref["content"])


if __name__ == "__main__":
    unittest.main()
