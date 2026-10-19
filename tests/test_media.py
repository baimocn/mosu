# -*- coding: utf-8 -*-
"""图片解密测试：V0 XOR 与 V2 容器（构造数据往返，不需要真实图片）。"""
import json
import os
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from Crypto.Cipher import AES  # noqa: E402

from core.media import V2_MAGIC, decode_dat, decode_v2, sniff_ext  # noqa: E402


class TestV0(unittest.TestCase):
    def test_xor_roundtrip(self):
        raw = bytes([0x00, 0x41, 0xFF, 0x3D])
        self.assertEqual(decode_dat(raw), bytes([0x3D, 0x7C, 0xC2, 0x00]))

    def test_double_xor_identity(self):
        raw = os.urandom(64)
        self.assertEqual(decode_dat(decode_dat(raw)), raw)


class TestSniff(unittest.TestCase):
    def test_extensions(self):
        self.assertEqual(sniff_ext(b"\xff\xd8\xff"), "jpg")
        self.assertEqual(sniff_ext(b"\x89PNG\r\n\x1a\n"), "png")
        self.assertEqual(sniff_ext(b"GIF89a"), "gif")
        self.assertEqual(sniff_ext(b"RIFF\x00\x00\x00\x00WEBP"), "webp")
        self.assertEqual(sniff_ext(b"\x00" * 16), "bin")


class TestV2(unittest.TestCase):
    KEY = bytes.fromhex("00112233445566778899aabbccddeeff")  # 测试用假密钥，非真实数据

    def _build(self, plain: bytes, aes_size: int = 1024) -> bytes:
        """按墨溯解析出的格式正向构造一个 V2 容器。"""
        head, tail = plain[:aes_size], plain[aes_size:]
        enc = AES.new(self.KEY, AES.MODE_ECB).encrypt(head)
        return (V2_MAGIC
                + struct.pack("<I", aes_size)
                + struct.pack("<I", len(tail))
                + b"\x00"                       # 1 字节间隔，凑到偏移 15
                + enc
                + b"\x00" * 16                  # 段间 16 字节
                + bytes(b ^ 0x3D for b in tail))

    def test_roundtrip(self):
        plain = os.urandom(1024 + 777)
        raw = self._build(plain)
        self.assertEqual(decode_v2(raw, self.KEY), plain)

    def test_small_payload(self):
        plain = os.urandom(1024 + 1)
        self.assertEqual(decode_v2(self._build(plain), self.KEY), plain)

    def test_wrong_key_returns_none_or_garbage(self):
        """key 不对时不得抛异常（返回什么都可以，但不能崩）。"""
        plain = os.urandom(1024 + 32)
        raw = self._build(plain)
        try:
            out = decode_v2(raw, b"\x00" * 16)
        except Exception as e:  # noqa: BLE001
            self.fail(f"decode_v2 抛异常了: {e}")
        if out is not None:
            self.assertNotEqual(out, plain)    # 错 key 不该碰巧解对

    def test_bad_magic(self):
        self.assertIsNone(decode_v2(b"not-a-v2-container" * 4, self.KEY))

    def test_truncated(self):
        plain = os.urandom(1024 + 32)
        raw = self._build(plain)
        self.assertIsNone(decode_v2(raw[:-20], self.KEY))


class TestMediaStoreIndex(unittest.TestCase):
    """MediaStore 索引：无后缀主图 + 缓存指纹失效（v0.2.0 后的真实坑）。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # 造假的 msg/attach/{hash}/{date}/Img/ 结构
        img_dir = self.root / "msg" / "attach" / "ab12" / "2026-10" / "Img"
        img_dir.mkdir(parents=True)
        self.md5 = "a" * 32
        # 三种命名：缩略图 _t、无后缀主图、带 _h
        (img_dir / f"{self.md5}_t.dat").write_bytes(b"t")
        (img_dir / f"{self.md5}.dat").write_bytes(b"main")
        (img_dir / f"{self.md5}_h.dat").write_bytes(b"h")
        # 噪声：非 32 位前缀的文件应被忽略
        (img_dir / "tooshort.dat").write_bytes(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, cache=True):
        from core.media import MediaStore
        return MediaStore(self.root, self.root / ".cache").build(use_cache=cache)

    def test_no_suffix_main_indexed(self):
        """无后缀主图 {md5}.dat 必须进索引（曾整个漏掉 6726 张）。"""
        ms = self._store()
        entry = ms.images.get(self.md5)
        self.assertIsNotNone(entry)
        self.assertIn("main", entry)
        self.assertIn("t", entry)
        self.assertIn("h", entry)
        self.assertNotIn("tooshort", ms.images)

    def test_cache_fingerprint_invalidation(self):
        """目录指纹变化（微信存了新图）→ 旧缓存必须失效重建。"""
        ms1 = self._store()
        self.assertIn("main", ms1.images[self.md5])
        # 新图落盘（更新目录 mtime 由文件系统保证；这里直接换缓存内容模拟过期）
        cache = self.root / ".cache" / "media_index.json"
        d = json.loads(cache.read_text(encoding="utf-8"))
        self.assertIn("fingerprint", d)
        d["fingerprint"] = 0.0                      # 伪装成过期指纹
        cache.write_text(json.dumps(d), encoding="utf-8")
        ms2 = self._store()                          # 应检测到指纹不符，重建
        self.assertIn("main", ms2.images[self.md5])
        d2 = json.loads(cache.read_text(encoding="utf-8"))
        self.assertNotEqual(d2["fingerprint"], 0.0)


if __name__ == "__main__":
    unittest.main()
