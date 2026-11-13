# -*- coding: utf-8 -*-
"""HTML 导出必须把表情图片一起带走，而不是只留 [表情] 文字。

历史缺陷：ChatExporter.render_message 的 T_EMOJI 分支写死输出文字占位，
且导出链路只传 MediaStore（不含表情缓存/解密）——GUI 里能显示的表情，
导出的 HTML 里全丢。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.exporter import ChatExporter  # noqa: E402
from core.msgparse import T_EMOJI  # noqa: E402


class _FakeMedia:
    def get_image(self, md5=None):
        return None


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
MD5 = "a" * 32


class TestEmoticonExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def _exp(self, getter=None):
        return ChatExporter(_FakeMedia(), self.out, emoticon_getter=getter)

    def test_without_getter_falls_back_to_text(self):
        exp = self._exp()
        html = exp.render_message({"type": T_EMOJI, "emoji_md5": MD5, "text": "x"})
        self.assertIn("[表情", html)
        self.assertNotIn("<img", html)

    def test_with_getter_renders_img_and_writes_file(self):
        exp = self._exp(lambda md5: (PNG, ".png", "emoticon-dec") if md5 == MD5 else None)
        html = exp.render_message({"type": T_EMOJI, "emoji_md5": MD5, "text": "x"})
        self.assertIn('alt="表情"', html)
        self.assertIn("<img", html)
        f = self.out / f"media/e_{MD5[:2]}/{MD5}.png"
        self.assertTrue(f.exists())
        self.assertEqual(f.read_bytes(), PNG)
        self.assertEqual(exp.stats["表情图片"], 1)

    def test_missing_image_keeps_explicit_placeholder(self):
        exp = self._exp(lambda md5: None)
        html = exp.render_message({"type": T_EMOJI, "emoji_md5": MD5, "text": "x"})
        self.assertIn("本地无可显示图片", html)
        self.assertNotIn("<img", html)

    def test_reuses_cached_file(self):
        calls = []

        def getter(md5):
            calls.append(md5)
            return (PNG, ".png", "emoticon-dec")

        exp = self._exp(getter)
        exp.render_message({"type": T_EMOJI, "emoji_md5": MD5, "text": "x"})
        exp.render_message({"type": T_EMOJI, "emoji_md5": MD5, "text": "x"})
        self.assertEqual(len(calls), 1, "同一 md5 不该重复解码/转码")

    def test_non_image_ext_is_rejected(self):
        exp = self._exp(lambda md5: (b"junk", ".bin", "x"))
        html = exp.render_message({"type": T_EMOJI, "emoji_md5": MD5, "text": "x"})
        self.assertNotIn("<img", html)


if __name__ == "__main__":
    unittest.main()
