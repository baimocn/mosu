# -*- coding: utf-8 -*-
"""导出目的地（dest）的离线单测。

用户原话：「导出时产物放哪不应该是可选的吗」。这组测试锁三件事：
  1. _resolve_dest：预设 key / 自定义绝对路径 / 各种非法输入的判定
  2. INDEX 登记簿：追加 → 读取 → 倒序的往返
  3. 预设目录清单：四项 + 存在性标记的结构

不起服务、不碰真实数据。下载端点的路径防穿越逻辑在 run_gui 闭包里，
由真机联调验证（见项目日记）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.server import (  # noqa: E402
    EXPORT_ROOT, _export_dests, _export_index_append, _export_index_path,
    _export_index_read, _resolve_dest,
)


class TestResolveDest(unittest.TestCase):
    def test_empty_and_project_default(self):
        self.assertEqual(_resolve_dest(""), EXPORT_ROOT)
        self.assertEqual(_resolve_dest(None), EXPORT_ROOT)
        self.assertEqual(_resolve_dest("project"), EXPORT_ROOT)

    def test_preset_keys_map_to_paths(self):
        for d in _export_dests():
            if d["key"] == "project" or not d["exists"]:
                continue
            self.assertEqual(_resolve_dest(d["key"]), Path(d["path"]))

    def test_custom_absolute_existing_dir(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_resolve_dest(td), Path(td))

    def test_custom_nonexistent_rejected(self):
        with self.assertRaises(ValueError) as cm:
            _resolve_dest("D:/__mosu_no_such_dir__")
        self.assertIn("不存在", str(cm.exception))

    def test_relative_path_rejected(self):
        with self.assertRaises(ValueError):
            _resolve_dest("some/relative/path")

    def test_traversal_rejected(self):
        """../ 必须死在「不是绝对路径」这道判据上，不能进到拼接那步。"""
        with self.assertRaises(ValueError):
            _resolve_dest("../../etc")


class TestExportDests(unittest.TestCase):
    def test_four_presets_with_shape(self):
        dests = _export_dests()
        keys = [d["key"] for d in dests]
        self.assertEqual(keys, ["project", "desktop", "downloads", "documents"])
        for d in dests:
            self.assertIn("label", d)
            self.assertIn("path", d)
            self.assertIsInstance(d["exists"], bool)


class TestExportIndex(unittest.TestCase):
    def setUp(self):
        # INDEX 落在 EXPORT_ROOT 里；测试指向临时 MOSU 家不够——
        # EXPORT_ROOT 是 import 期常量，只能备份真文件、测完还原。
        self._path = _export_index_path()
        self._backup = self._path.read_bytes() if self._path.exists() else None
        if self._backup is not None:
            self._path.unlink()

    def tearDown(self):
        if self._path.exists():
            self._path.unlink()
        if self._backup is not None:
            self._path.write_bytes(self._backup)

    def test_append_and_read_reverse(self):
        for i in range(3):
            _export_index_append({"task": f"tid{i}", "session": f"会话{i}",
                                  "dest": "D:/tmp", "bytes": i,
                                  "mtime": "10-25 00:0%d" % i,
                                  "files": [{"name": f"f{i}.csv", "size": i}]})
        rows = _export_index_read()
        self.assertEqual([r["task"] for r in rows], ["tid2", "tid1", "tid0"])
        self.assertEqual(rows[0]["files"][0]["name"], "f2.csv")

    def test_corrupt_lines_skipped(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"task": "ok1"}, ensure_ascii=False) + "\n")
            f.write("这不是 json\n")
            f.write("\n")
        rows = _export_index_read()
        self.assertEqual([r["task"] for r in rows], ["ok1"])


if __name__ == "__main__":
    unittest.main()
