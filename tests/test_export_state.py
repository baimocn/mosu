# -*- coding: utf-8 -*-
"""增量导出状态管理测试（离线，用临时目录）。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import export_state as es  # noqa: E402


def _msg(i, ts):
    return {"localId": i, "createTime": ts, "content": f"m{i}"}


class TestState(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            es.save_state(td, "wxid_x", 1760000000, 42)
            st = es.load_state(td)
            self.assertEqual(st["sessionId"], "wxid_x")
            self.assertEqual(st["lastExportTime"], 1760000000)
            self.assertEqual(st["count"], 42)
            self.assertIn("exportedAt", st)

    def test_load_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(es.load_state(td))

    def test_load_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            es.state_path(td).write_text("{broken", encoding="utf-8")
            self.assertIsNone(es.load_state(td))


class TestMerge(unittest.TestCase):
    def test_dedup_and_sort(self):
        old = [_msg(1, 100), _msg(2, 200)]
        new = [_msg(2, 200), _msg(3, 300)]
        merged, added = es.merge_messages(old, new)
        self.assertEqual([m["localId"] for m in merged], [1, 2, 3])
        self.assertEqual(added, 1)

    def test_empty_old(self):
        merged, added = es.merge_messages([], [_msg(1, 10)])
        self.assertEqual((len(merged), added), (1, 1))

    def test_no_new(self):
        merged, added = es.merge_messages([_msg(1, 10)], [_msg(1, 10)])
        self.assertEqual((len(merged), added), (1, 0))

    def test_out_of_order_new(self):
        merged, added = es.merge_messages([_msg(1, 100)], [_msg(3, 300), _msg(2, 200)])
        self.assertEqual([m["localId"] for m in merged], [1, 2, 3])


class TestAppend(unittest.TestCase):
    def test_text_append(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.txt"
            self.assertEqual(es.append_text(p, ["x", "y"]), 2)
            self.assertEqual(es.append_text(p, ["z"]), 1)
            self.assertEqual(p.read_text(encoding="utf-8"), "x\ny\nz\n")

    def test_text_append_empty(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.txt"
            self.assertEqual(es.append_text(p, []), 0)
            self.assertFalse(p.exists())

    def test_csv_append_header_once(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.csv"
            es.append_csv(p, [["2026-01-01", "out", "我", "text", "hi"]])
            es.append_csv(p, [["2026-01-02", "in", "他", "text", "yo"]])
            lines = p.read_text(encoding="utf-8-sig").splitlines()
            self.assertEqual(len(lines), 3)              # 表头只出现一次
            self.assertIn("时间", lines[0])
            self.assertIn("2026-01-02", lines[2])

    def test_json_merge_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.json"
            old = {"session": {"sessionId": "s1"}, "messages": [_msg(1, 100)]}
            p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
            new = {"session": {"sessionId": "s1"}, "messages": [_msg(1, 100), _msg(2, 200)]}
            out, added = es.merge_json_file(p, new)
            self.assertEqual(added, 1)
            self.assertEqual([m["localId"] for m in out["messages"]], [1, 2])
            self.assertEqual(out["session"]["messageCount"], 2)
            self.assertEqual(out["incremental"]["added"], 1)


if __name__ == "__main__":
    unittest.main()
