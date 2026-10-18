# -*- coding: utf-8 -*-
"""SQLCipher4 解密核心测试（离线，不需要微信数据）。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.sqlcipher4 import (  # noqa: E402
    PAGE_SIZE, RESERVE, DATA_END, IV_OFF, HMAC_OFF, MAGIC,
    derive_enc_key,
)


class TestLayout(unittest.TestCase):
    def test_page_layout(self):
        self.assertEqual(PAGE_SIZE, 4096)
        self.assertEqual(RESERVE, 80)
        self.assertEqual(DATA_END, 4016)
        self.assertEqual(IV_OFF, 4016)
        self.assertEqual(HMAC_OFF, 4032)
        self.assertEqual(MAGIC, b"SQLite format 3\x00")


class TestDerive(unittest.TestCase):
    def test_deterministic(self):
        a = derive_enc_key(bytes(range(32)), bytes(range(16)))
        b = derive_enc_key(bytes(range(32)), bytes(range(16)))
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)          # AES-256 key

    def test_known_vectors(self):
        """防止回归：参数改错（reserve/kdf 次数）这里立刻炸。"""
        self.assertEqual(
            derive_enc_key(bytes(range(32)), bytes(range(16))).hex(),
            "d6d2e116eae459f966d0f24ffeba9c0eca5f0c66c4574816378c04dae2425862")
        self.assertEqual(
            derive_enc_key(b"p" * 32, b"s" * 16).hex(),
            "c82d5d7f029a297c833d0b6455c55c49f9f513c56252b4f87727d800f5993af2")

    def test_salt_matters(self):
        self.assertNotEqual(derive_enc_key(b"k" * 32, b"s" * 16),
                            derive_enc_key(b"k" * 32, b"t" * 16))


if __name__ == "__main__":
    unittest.main()
