# -*- coding: utf-8 -*-
"""中文情感打分测试（词典 + 否定反转 + 程度加权）。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.sentiment import score_text, score_texts, sentiment_curve  # noqa: E402


class TestScore(unittest.TestCase):
    def test_positive(self):
        s, p, n = score_text("哈哈哈哈太开心了")
        self.assertGreater(s, 0.5)
        self.assertTrue(p)
        self.assertFalse(n)

    def test_negative(self):
        s, _, _ = score_text("今天真的好累好想哭")
        self.assertLess(s, -0.5)

    def test_negation_flips(self):
        s_pos, _, _ = score_text("开心")
        s_neg, _, _ = score_text("不开心")
        self.assertGreater(s_pos, 0)
        self.assertLess(s_neg, 0)

    def test_neutral(self):
        s, p, n = score_text("晚安")
        self.assertEqual(s, 0.0)
        self.assertEqual((p, n), ([], []))

    def test_empty(self):
        self.assertEqual(score_text(""), (0.0, [], []))

    def test_range(self):
        for t in ["爱死了", "烦死了", "谢谢谢谢谢谢", "讨厌讨厌讨厌", "一般吧"]:
            s, _, _ = score_text(t)
            self.assertGreaterEqual(s, -1.0)
            self.assertLessEqual(s, 1.0)


class TestBatch(unittest.TestCase):
    def test_counts(self):
        r = score_texts(["哈哈开心", "好累", "谢谢", "晚安"])
        self.assertEqual(r["positive"] + r["negative"] + r["neutral"], 4)
        self.assertGreaterEqual(r["positiveRate"], 0.0)
        self.assertLessEqual(r["positiveRate"], 1.0)
        self.assertIn("avg", r)

    def test_top_words(self):
        r = score_texts(["哈哈开心哈哈", "累"])
        self.assertTrue(any(w["word"] == "哈哈" for w in r["topPositiveWords"]))


class TestCurve(unittest.TestCase):
    def test_buckets(self):
        # 2026-01-15 10:00 与 2026-02-20 10:00（本地时区）
        import time
        t1 = int(time.mktime(time.strptime("2026-01-15 10:00", "%Y-%m-%d %H:%M")))
        t2 = int(time.mktime(time.strptime("2026-02-20 10:00", "%Y-%m-%d %H:%M")))
        curve = sentiment_curve([(t1, "开心"), (t2, "好累")], bucket="month")
        self.assertEqual([c["bucket"] for c in curve], ["2026-01", "2026-02"])
        self.assertGreater(curve[0]["avg"], 0)
        self.assertLess(curve[1]["avg"], 0)


if __name__ == "__main__":
    unittest.main()
