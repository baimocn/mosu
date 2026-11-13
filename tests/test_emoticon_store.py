# -*- coding: utf-8 -*-
"""本地明文表情缓存目录（md5 命名）的只读查找契约。

微信 4.x 的 business/emoticon/ 缓存是私有加密容器，项目没有解密器。
本机常有一份从 CDN 下载好的**明文**表情（文件名即 md5），用作兜底显示。
这里锁住：只读、只认 32hex、非图片不喂给 <img>、找不到返回 None。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.media import EmoticonStore  # noqa: E402

# 1×1 透明 GIF 的最小合法字节
GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
       b"\x21\xf9\x04\x00\x00\x00\x00\x00\x2c\x00\x00\x00\x00"
       b"\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b")
MD5 = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


class TestEmoticonStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "Emojis"
        self.dir.mkdir()
        (self.dir / (MD5 + ".gif")).write_bytes(GIF)
        self.store = EmoticonStore(dirs=[self.dir])

    def test_hit_returns_image_bytes(self):
        got = self.store.get(MD5)
        self.assertIsNotNone(got)
        data, ext, source = got
        self.assertEqual(data, GIF)
        self.assertEqual(ext, ".gif")
        self.assertEqual(source, "emoticon")

    def test_case_insensitive_md5(self):
        self.assertIsNotNone(self.store.get(MD5.upper()))

    def test_rejects_invalid_md5(self):
        for bad in ("", None, "short", "z" * 32, MD5[:31]):
            self.assertIsNone(self.store.get(bad))

    def test_missing_returns_none(self):
        self.assertIsNone(self.store.get("0" * 32))

    def test_non_image_is_rejected(self):
        """目录里放了非图片，也不能当表情喂给前端。"""
        p = self.dir / ("a" * 32 + ".gif")
        p.write_bytes(b"not an image at all")
        store = EmoticonStore(dirs=[self.dir])
        self.assertIsNone(store.get("a" * 32))

    def test_no_hardcoded_personal_path(self):
        """源码里不许出现具体用户目录（仓库公开、路径要可配置）。"""
        src = (Path(__file__).resolve().parents[1] / "core" / "media.py").read_text(
            encoding="utf-8")
        self.assertNotIn("wechat-scan", src)
        self.assertIn("MOSU_EMOTICON_DIRS", src)


if __name__ == "__main__":
    unittest.main()
