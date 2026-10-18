# -*- coding: utf-8 -*-
"""图片解密测试：V0 XOR 与 V2 容器（构造数据往返，不需要真实图片）。"""
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


if __name__ == "__main__":
    unittest.main()
