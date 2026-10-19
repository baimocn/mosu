# -*- coding: utf-8 -*-
"""GUI 导出资质的离线单测（不碰真实数据，全 stub）。

覆盖 gui/server.py 的三个模块级设施：
  - _text_export_bytes：json/csv/txt 三格式与 CLI 输出的一致性
  - _zip_dir：arcname 相对化 + 防穿越
  - _TaskHub：异步任务状态机 + 产物路径
"""
import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime  # noqa: E402

from gui.server import _TaskHub, _text_export_bytes, _zip_dir  # noqa: E402

# fmt_time 走本地时区；测试期望值必须同源计算，不能写死字符串
# （否则 UTC 的 CI 会挂——v0.2.0 发布后就踩过这个坑）
_T = 1770000000
_TS = datetime.fromtimestamp(_T).strftime("%Y-%m-%d %H:%M:%S")
_TS3 = datetime.fromtimestamp(_T + 120).strftime("%Y-%m-%d %H:%M:%S")  # 第三条（voice）


class _StubMessages:
    """duck-typed ds.messages()：返回与 Dataset.messages() 同构的罐头数据。"""

    def messages(self, sess, limit=50, offset=0, order="desc", **kw):
        msgs = [
            {"createTime": _T, "direction": "out", "senderName": "白墨",
             "kind": "text", "content": "在吗"},
            {"createTime": 1770000060, "direction": "in", "senderName": "张三",
             "kind": "image", "content": "", "imgMd5": "a" * 32},
            {"createTime": 1770000120, "direction": "in", "senderName": "张三",
             "kind": "voice", "content": "", "voiceMs": 5000,
             "voiceText": "我也在"},
            {"createTime": 1770000180, "direction": "out", "senderName": "白墨",
             "kind": "file", "content": "", "title": "报表.xlsx",
             "fileName": "报表.xlsx"},
        ]
        return {"session": {"sessionId": sess, "displayName": "张三"},
                "messages": msgs, "cursor": None}


class TestTextExportBytes(unittest.TestCase):
    def setUp(self):
        self.ds = _StubMessages()
        self.sess = {"sessionId": "wxid_x", "displayName": "张三"}

    def test_csv_header_and_bom(self):
        data, ext = _text_export_bytes(self.ds, self.sess, "csv")
        self.assertEqual(ext, "csv")
        text = data.decode("utf-8-sig")          # 带 BOM，Excel 友好
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "时间,方向,发送者,类型,内容")
        self.assertEqual(len(lines), 5)           # 表头 + 4 条
        self.assertIn("在吗", lines[1])
        # CSV 的内容列直接用 content（voice 的 content 为空，voiceText 只进 TXT）
        self.assertEqual(lines[3], f"{_TS3},in,张三,voice,")

    def test_txt_line_format(self):
        data, ext = _text_export_bytes(self.ds, self.sess, "txt")
        self.assertEqual(ext, "txt")
        text = data.decode("utf-8")
        # 期望时间与 fmt_time 同源（本地时区），CI/本机都过
        self.assertIn(f"[{_TS}] 我: 在吗", text)
        self.assertIn("张三: [图片]", text)
        self.assertIn("张三: [语音 5000ms] 我也在", text)
        self.assertIn("我: [文件] 报表.xlsx", text)

    def test_json_roundtrip(self):
        data, ext = _text_export_bytes(self.ds, self.sess, "json")
        self.assertEqual(ext, "json")
        r = json.loads(data.decode("utf-8"))
        self.assertEqual(len(r["messages"]), 4)
        self.assertEqual(r["session"]["displayName"], "张三")

    def test_unknown_format_raises(self):
        with self.assertRaises(ValueError):
            _text_export_bytes(self.ds, self.sess, "xml")


class TestZipDir(unittest.TestCase):
    def test_relative_arcnames_and_readable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_text("<html>x</html>", encoding="utf-8")
            sub = root / "media" / "ab"
            sub.mkdir(parents=True)
            (sub / "deadbeef.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")
            data = _zip_dir(root)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
                self.assertEqual(names, {"index.html", "media/ab/deadbeef.jpg"})
                self.assertIsNone(zf.testzip())     # 完整性
                self.assertIn(b"<html>x</html>", zf.read("index.html"))


class TestTaskHub(unittest.TestCase):
    def test_state_machine_and_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            hub = _TaskHub(Path(td), keep=3)
            tid = hub.submit("test", lambda _: {"ok": True})
            t = hub.fetch(tid)
            self.assertIn(t["status"], ("queued", "running"))
            for _ in range(50):                     # 等 worker 跑完（<1s）
                t = hub.fetch(tid)
                if t["status"] == "done":
                    break
                time.sleep(0.05)
            self.assertEqual(t["status"], "done")
            self.assertEqual(t["result"], {"ok": True})
            self.assertIsNotNone(t["cost"])
            self.assertIsNone(hub.fetch("nonexistent"))

    def test_error_status(self):
        with tempfile.TemporaryDirectory() as td:
            hub = _TaskHub(Path(td))
            def boom(_):
                raise RuntimeError("炸了")
            tid = hub.submit("test", boom)
            for _ in range(50):
                t = hub.fetch(tid)
                if t["status"] == "error":
                    break
                time.sleep(0.05)
            self.assertEqual(t["status"], "error")
            self.assertIn("RuntimeError", t["error"])


if __name__ == "__main__":
    unittest.main()
