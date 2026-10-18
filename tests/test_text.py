# -*- coding: utf-8 -*-
"""文本清洗 / 分词 / 输出格式 / 配置解析链测试（全部离线）。"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.analytics import clean_text, extract_keywords  # noqa: E402
from core.dataset import parse_time, find_user_dir  # noqa: E402
from core.formatters import emit  # noqa: E402


class TestCleanText(unittest.TestCase):
    def test_xml_message(self):
        self.assertEqual(clean_text("<msg><emoji md5='x'/></msg>"), "")

    def test_internal_id(self):
        self.assertEqual(clean_text("com.tencent.xin.emoticon.person.stiker_17280338773"), "")
        self.assertEqual(clean_text("a1b2c3d4e5f6_17280338773"), "")

    def test_emoji_placeholder(self):
        self.assertEqual(clean_text("[旺柴]"), "")
        self.assertEqual(clean_text("你好[捂脸]啊"), "你好啊")

    def test_plain_kept(self):
        self.assertEqual(clean_text("  明天去图书馆吗  "), "明天去图书馆吗")

    def test_none(self):
        self.assertEqual(clean_text(None), "")


class TestKeywords(unittest.TestCase):
    def test_no_id_noise(self):
        texts = ["我的微信号是 wxid_abc123456789 记得加", "python 写脚本真香"]
        kw = dict(extract_keywords(texts, 20))
        self.assertNotIn("wxid_abc123456789", kw)

    def test_xml_not_counted(self):
        kw = dict(extract_keywords(["<emoji productid='com.tencent.xin'/>"] * 5, 20))
        self.assertNotIn("com", kw)


class TestParseTime(unittest.TestCase):
    def test_formats(self):
        import time as _t
        self.assertEqual(parse_time("2026-01-02"),
                         int(_t.mktime(_t.strptime("2026-01-02 00:00", "%Y-%m-%d %H:%M"))))
        self.assertEqual(parse_time("2026-01-02 03:04:05"),
                         int(_t.mktime(_t.strptime("2026-01-02 03:04:05", "%Y-%m-%d %H:%M:%S"))))
        self.assertEqual(parse_time(1700000000), 1700000000)

    def test_empty(self):
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time(""))


class TestFindUserDir(unittest.TestCase):
    def test_env_priority(self):
        with tempfile.TemporaryDirectory() as td:
            acc = Path(td) / "wxid_test"
            (acc / "db_storage").mkdir(parents=True)
            old = os.environ.get("MOSU_DB_PATH")
            os.environ["MOSU_DB_PATH"] = str(acc)
            try:
                self.assertEqual(find_user_dir(), acc)
                # 显式参数优先于环境变量
                other = Path(td) / "other"
                (other / "db_storage").mkdir(parents=True)
                self.assertEqual(find_user_dir(other), other)
            finally:
                if old is None:
                    os.environ.pop("MOSU_DB_PATH", None)
                else:
                    os.environ["MOSU_DB_PATH"] = old

    def test_db_storage_itself(self):
        with tempfile.TemporaryDirectory() as td:
            ds = Path(td) / "db_storage"
            ds.mkdir()
            self.assertEqual(find_user_dir(ds), ds.parent)


class TestFormatters(unittest.TestCase):
    ROWS = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]

    def _cap(self, fmt, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            emit(self.ROWS, fmt, **kw)
        return buf.getvalue()

    def test_json(self):
        out = self._cap("json", raw={"k": 1})
        d = json.loads(out)
        self.assertEqual(d["k"], 1)

    def test_jsonl(self):
        out = self._cap("jsonl")
        self.assertEqual(len([l for l in out.splitlines() if l.strip()]), 2)

    def test_table(self):
        out = self._cap("table")
        self.assertIn("a", out)
        self.assertIn("x", out)

    def test_csv(self):
        out = self._cap("csv")
        self.assertIn("a,b", out)

    def test_markdown(self):
        out = self._cap("markdown")
        self.assertIn("| a | b |", out)


if __name__ == "__main__":
    unittest.main()
