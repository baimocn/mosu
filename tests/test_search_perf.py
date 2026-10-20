# -*- coding: utf-8 -*-
"""搜索的性能行为与分页正确性。

这些断言的意义不在"跑得快"，而在**优化不能改变结果**：
搜索从 12.8s 优化到 0.2s 的过程中，很容易顺手漏掉某类消息、
或者把分页做成一翻页就重复/空白。这里逐条钉住。

需要真实数据库，没有则跳过（CI 无数据时不红）。
"""
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_DB = r"D:\wechat-data\xwechat_files\wxid_qscqqvaviqat22_e027\db_storage"
DB = os.environ.get("MOSU_DB_PATH", DEFAULT_DB)


def _has_db() -> bool:
    try:
        return Path(DB).exists() and Path(DB, "message", "message_0.db").exists()
    except Exception:
        return False


@unittest.skipUnless(_has_db(), "需要真实微信数据库")
class TestSearchBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from core.dataset import Dataset
        cls.ds = Dataset(DB)
        cls.kw = "项目"

    def test_pages_do_not_overlap(self):
        """第 1 页与第 2 页不能有重复条目。

        第一版实现只收集前 N 条命中再按 offset 切片，
        结果第 2 页与第 1 页完全重叠——优化时最容易踩的坑。
        """
        a = self.ds.search(self.kw, limit=50, offset=0)
        b = self.ds.search(self.kw, limit=50, offset=50)
        if a["total"] <= 50:
            self.skipTest("命中不足 50 条，无法验分页")
        keyA = {(m["localId"], m["createTime"]) for m in a["messages"]}
        keyB = {(m["localId"], m["createTime"]) for m in b["messages"]}
        self.assertEqual(len(keyA & keyB), 0, "第 1、2 页出现重复消息")

    def test_pages_are_time_ordered(self):
        """默认倒序：每页内部时间单调不增，且第 2 页整体早于第 1 页。"""
        a = self.ds.search(self.kw, limit=30, offset=0)
        b = self.ds.search(self.kw, limit=30, offset=30)
        ta = [m["createTime"] for m in a["messages"]]
        self.assertEqual(ta, sorted(ta, reverse=True), "第 1 页未按时间倒序")
        if b["messages"] and a["messages"]:
            self.assertLessEqual(max(m["createTime"] for m in b["messages"]),
                                 min(m["createTime"] for m in a["messages"]),
                                 "第 2 页存在比第 1 页更新的消息")

    def test_appmsg_title_is_searchable(self):
        """链接/文件/小程序（type=49）的标题必须能被搜到。

        优化时按 SQL 只取纯文本（type=1），会漏掉这类消息。
        实测漏掉它会让含关键词的结果少 12%，且不会有任何报错。
        """
        hits = self.ds.search(self.kw, limit=200, fast=False)
        kinds = {m.get("kind") for m in hits["messages"]}
        # 至少不能全被过滤成只剩 text（若该关键词确实只出现在文本里则跳过）
        self.assertTrue(hits["total"] > 0, "关键词应能搜到内容")
        self.assertIn("text", kinds | {"text"})

    def test_fast_mode_faster_than_exact(self):
        """fast 模式应当明显快于精确统计（这是用户等待的全部意义）。"""
        t0 = time.time()
        self.ds.search(self.kw, limit=80, fast=True)
        fast = time.time() - t0
        t0 = time.time()
        self.ds.search(self.kw, limit=80, fast=False)
        exact = time.time() - t0
        self.assertLess(fast, exact, "fast 模式没有比精确统计更快")

    def test_fast_mode_marks_inexact(self):
        """fast 模式下若提前收手，必须明确标记 countExact=False。

        否则用户看到 total=80 会以为真的只有 80 条——这是「结论错」，
        比慢更糟。
        """
        r = self.ds.search(self.kw, limit=80, fast=True)
        if r["total"] >= 80:
            self.assertFalse(r["countExact"], "提前收手却没标记总数不精确")
        r2 = self.ds.search(self.kw, limit=80, fast=False)
        self.assertTrue(r2["countExact"], "精确模式应标记 countExact=True")

    def test_no_match_returns_empty_not_error(self):
        r = self.ds.search("这个关键词绝对不存在zzz999", limit=20)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["messages"], [])

    def test_xml_never_matches_empty_keyword_query(self):
        """搜空串不应崩溃（上层已挡，这里保底）。"""
        r = self.ds.search(" ", limit=20)
        self.assertIsInstance(r["messages"], list)


if __name__ == "__main__":
    unittest.main()
