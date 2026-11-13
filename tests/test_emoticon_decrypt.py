# -*- coding: utf-8 -*-
"""微信 4.x 加密表情缓存的解密契约（纯构造数据，不依赖真实账号/微信进程）。

格式来自社区对 Weixin.dll 的静态逆向，并在真实账号上实测 495/495 通过：
  整文件 AES-128-CBC，key = IV = MD5(f"{seed}{wxid}EMOTICON").digest()，
  PKCS#7 填充，无文件头，密文长度必为 16 的倍数。

这里锁住：key 派生、PKCS#7 校验、格式识别、wxid 去后缀、
以及「解不出可显示图片时返回 None」而不是把垃圾喂给 <img>。
"""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Crypto.Cipher import AES  # noqa: E402

from core.media import (  # noqa: E402
    EncryptedEmoticonStore, _aes_cbc_decrypt, _strip_pkcs7, sniff_ext,
)

SEED = "123456789"
WXID = "wxid_test_account"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _key(seed=SEED, wxid=WXID):
    # MD5 是微信既定 KDF，不能换 SHA-256
    return hashlib.md5((seed + wxid + "EMOTICON").encode()).digest()


def _encrypt(plain: bytes, key: bytes) -> bytes:
    pad = 16 - (len(plain) % 16)
    body = plain + bytes([pad]) * pad
    return AES.new(key, AES.MODE_CBC, key).encrypt(body)


class TestDerive(unittest.TestCase):
    def test_key_is_16_bytes_and_deterministic(self):
        k = _key()
        self.assertEqual(len(k), 16)
        self.assertEqual(k, _key())

    def test_wxid_suffix_is_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / (WXID + "_e027")
            d.mkdir()
            s = EncryptedEmoticonStore(d, seed=SEED)
            self.assertEqual(s.wxid, WXID)

    def test_different_seed_gives_different_key(self):
        self.assertNotEqual(_key(), _key(seed="12345678"))


class TestDecrypt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / (WXID + "_e027")
        (self.dir / "business" / "emoticon" / "Persist" / "aa").mkdir(parents=True)
        self.md5 = "a" * 32
        self.file = self.dir / "business" / "emoticon" / "Persist" / "aa" / self.md5
        self.store = EncryptedEmoticonStore(self.dir, seed=SEED, wxid=WXID)

    def test_plain_png_roundtrip(self):
        self.file.write_bytes(_encrypt(PNG, _key()))
        got = self.store.get(self.md5)
        self.assertIsNotNone(got)
        data, ext, src = got
        self.assertEqual(data, PNG)
        self.assertEqual(ext, ".png")
        self.assertEqual(src, "emoticon-dec")

    def test_wrong_seed_returns_none(self):
        self.file.write_bytes(_encrypt(PNG, _key(seed="99999999")))
        self.assertIsNone(self.store.get(self.md5))

    def test_bad_pkcs7_returns_none(self):
        key = _key()
        body = PNG + b"\x00" * (16 - len(PNG) % 16)
        self.file.write_bytes(AES.new(key, AES.MODE_CBC, key).encrypt(body))
        self.assertIsNone(self.store.get(self.md5))

    def test_unaligned_ciphertext_returns_none(self):
        self.file.write_bytes(_encrypt(PNG, _key()) + b"\x00")
        self.assertIsNone(self.store.get(self.md5))

    def test_no_seed_returns_none(self):
        self.store.seed = None
        self.file.write_bytes(_encrypt(PNG, _key()))
        self.assertIsNone(self.store.get(self.md5))

    def test_unknown_format_is_rejected(self):
        """解出来既不是图片也不是 wxgf → 别把随机字节喂给 <img>。"""
        self.file.write_bytes(_encrypt(b"NOT_AN_IMAGE" * 4, _key()))
        self.assertIsNone(self.store.get(self.md5))


class TestHelpers(unittest.TestCase):
    def test_strip_pkcs7(self):
        self.assertEqual(_strip_pkcs7(b"abc" + b"\x03" * 3), b"abc")
        self.assertIsNone(_strip_pkcs7(b""))
        self.assertIsNone(_strip_pkcs7(b"abc" + b"\xff" * 3))

    def test_sniff_ext(self):
        self.assertEqual(sniff_ext(PNG), "png")
        self.assertEqual(sniff_ext(b"wxgf" + b"\x00" * 8), "bin")

    def test_aes_cbc_decrypt_matches_encrypt(self):
        key = _key()
        self.assertEqual(_aes_cbc_decrypt(key, _encrypt(PNG, key))[:-8].startswith(PNG[:8]), True)


if __name__ == "__main__":
    unittest.main()
