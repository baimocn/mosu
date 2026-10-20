# -*- coding: utf-8 -*-
"""结果缓存的正确性。

这类缓存最危险的失败不是崩溃，是**返回过期数据而不报错**——
用户导入新的聊天记录后看到旧统计，会以为工具算错了。
所以测试重点全在"数据变了必须失效"。
"""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.cache import ResultCache, dataset_signature  # noqa: E402


class TestBasicBehaviour(unittest.TestCase):
    def test_set_get(self):
        c = ResultCache(maxsize=4, ttl=60)
        c.set(("a",), {"v": 1})
        self.assertEqual(c.get(("a",)), {"v": 1})

    def test_miss_returns_none(self):
        c = ResultCache()
        self.assertIsNone(c.get(("nope",)))

    def test_evicts_lru(self):
        c = ResultCache(maxsize=2, ttl=60)
        c.set("a", 1)
        c.set("b", 2)
        c.get("a")          # a 变为最近使用
        c.set("c", 3)       # 应淘汰 b
        self.assertIsNotNone(c.get("a"))
        self.assertIsNone(c.get("b"))
        self.assertIsNotNone(c.get("c"))

    def test_ttl_expiry(self):
        c = ResultCache(maxsize=4, ttl=0.05)
        c.set("k", "v")
        self.assertEqual(c.get("k"), "v")
        time.sleep(0.08)
        self.assertIsNone(c.get("k"), "过期后仍返回旧值")

    def test_stats(self):
        c = ResultCache(maxsize=4, ttl=60)
        c.set("k", 1)
        c.get("k")
        c.get("miss")
        s = c.stats()
        self.assertEqual(s["hits"], 1)
        self.assertEqual(s["misses"], 1)
        self.assertEqual(s["hitRate"], 0.5)


class TestSignatureInvalidation(unittest.TestCase):
    """签名是缓存失效的唯一依据，必须真的随数据变化。"""

    class FakeDS:
        def __init__(self, sessions):
            self._sessions = sessions

        def sessions(self):
            return self._sessions

    def test_stable_for_same_data(self):
        ds = self.FakeDS([{"sessionId": "a", "messageCount": 10, "lastTime": 100}])
        self.assertEqual(dataset_signature(ds), dataset_signature(ds))

    def test_changes_when_message_count_changes(self):
        """导入新消息后必须换签名，否则会返回过期统计。"""
        d1 = self.FakeDS([{"sessionId": "a", "messageCount": 10, "lastTime": 100}])
        d2 = self.FakeDS([{"sessionId": "a", "messageCount": 11, "lastTime": 100}])
        self.assertNotEqual(dataset_signature(d1), dataset_signature(d2))

    def test_changes_when_new_session(self):
        d1 = self.FakeDS([{"sessionId": "a", "messageCount": 10, "lastTime": 100}])
        d2 = self.FakeDS([{"sessionId": "a", "messageCount": 10, "lastTime": 100},
                          {"sessionId": "b", "messageCount": 5, "lastTime": 200}])
        self.assertNotEqual(dataset_signature(d1), dataset_signature(d2))

    def test_order_independent(self):
        """会话顺序变了但内容一样，不应视为数据变化（否则白丢缓存）。"""
        d1 = self.FakeDS([{"sessionId": "a", "messageCount": 1, "lastTime": 1},
                          {"sessionId": "b", "messageCount": 2, "lastTime": 2}])
        d2 = self.FakeDS([{"sessionId": "b", "messageCount": 2, "lastTime": 2},
                          {"sessionId": "a", "messageCount": 1, "lastTime": 1}])
        self.assertEqual(dataset_signature(d1), dataset_signature(d2))

    def test_broken_dataset_gives_fresh_signature(self):
        """拿不到索引时必须给一个每次都不同的值——宁可缓存不命中，
        也不能命中错的数据。"""
        class Broken:
            def sessions(self):
                raise RuntimeError("boom")

        a = dataset_signature(Broken())
        time.sleep(0.002)
        b = dataset_signature(Broken())
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("unknown-"))


class TestConcurrency(unittest.TestCase):
    def test_thread_safe(self):
        """GUI 是多线程的，缓存必须扛得住并发读写。"""
        c = ResultCache(maxsize=64, ttl=60)
        errors = []

        def worker(n):
            try:
                for i in range(200):
                    c.set((n, i), i)
                    c.get((n, i))
                    c.get(("shared", 0))
            except Exception as e:      # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [], f"并发访问报错: {errors[:3]}")
        self.assertLessEqual(c.stats()["size"], 64)


if __name__ == "__main__":
    unittest.main()
