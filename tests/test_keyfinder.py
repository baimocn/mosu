# -*- coding: utf-8 -*-
"""密钥提取器离线单测（不碰微信进程，全合成 fixture）。

覆盖三件事：PE 节表解析、掩码静态提取、SQLCipher 4 页校验判据。
真机取密钥靠 `mosu key find` 实测（无法在 CI 里跑，那里没有微信）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.keyfinder import (  # noqa: E402
    extract_xor_masks, pe_executable_sections, _derive_and_check,
    read_db_first_page, KeyFinderError, KEY_SIZE, KDF_ITER,
)


def build_pe(tmp: Path, sections: list[tuple[str, bytes, bool]]) -> Path:
    """合成一个最小 PE：只有节头和节内容，够 keyfinder 解析用。"""
    nsec = len(sections)
    opt_size = 240                       # PE32+ optional header 常见大小
    coff = 0x40 + 4                      # e_lfanew(0x40) 处是 "PE\0\0"，其后才是 COFF
    sec_off = coff + 20 + opt_size
    hdr = bytearray(sec_off + nsec * 40)
    hdr[0:2] = b"MZ"
    struct.pack_into("<I", hdr, 0x3C, 0x40)
    hdr[0x40:0x44] = b"PE\x00\x00"
    struct.pack_into("<H", hdr, coff + 2, nsec)           # NumberOfSections
    struct.pack_into("<H", hdr, coff + 16, opt_size)      # SizeOfOptionalHeader
    struct.pack_into("<H", hdr, coff + 20, 0x20B)         # PE32+ magic

    body = bytearray()
    for i, (name, data, executable) in enumerate(sections):
        off = sec_off + nsec * 40 + len(body)
        b = sec_off + i * 40
        hdr[b:b + 8] = name.encode().ljust(8, b"\x00")
        struct.pack_into("<IIII", hdr, b + 8, len(data), 0x1000 * (i + 1),
                         len(data), off)
        struct.pack_into("<I", hdr, b + 36,
                         0x20000000 if executable else 0x40000040)
        body += data
    out = tmp / "fake.dll"
    out.write_bytes(bytes(hdr) + bytes(body))
    return out


def mov_rdx_imm64(imm: bytes) -> bytes:
    return b"\x48\xBA" + imm


class TestPeParsing(unittest.TestCase):
    def test_only_executable_sections(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = build_pe(Path(d), [(".text", b"\x90" * 64, True),
                                   (".rdata", b"A" * 64, False)])
            secs = pe_executable_sections(p)
            self.assertEqual(len(secs), 1)
            self.assertEqual(secs[0][0], ".text")
            self.assertEqual(secs[0][2], 64)          # 大小

    def test_not_a_pe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.bin"
            p.write_bytes(b"hello world")
            self.assertEqual(pe_executable_sections(p), [])


class TestMaskExtraction(unittest.TestCase):
    def test_strict_pattern(self):
        import tempfile
        mask = bytes(range(32))
        code = (mov_rdx_imm64(mask[0:8]) + b"\x48\x89\x10"
                + mov_rdx_imm64(mask[8:16]) + b"\x48\x89\x50\x08"
                + mov_rdx_imm64(mask[16:24]) + b"\x48\x89\x50\x10"
                + mov_rdx_imm64(mask[24:32]) + b"\x48\x89\x50\x18"
                + b"\x48\x85\xC0")
        with tempfile.TemporaryDirectory() as d:
            p = build_pe(Path(d), [(".text", code + b"\x90" * 256, True)])
            masks = extract_xor_masks(p)
            self.assertIn(mask, masks)

    def test_loose_fallback_when_no_test_rax(self):
        import tempfile
        mask = bytes([0x5A] * 32)
        code = (mov_rdx_imm64(mask[0:8]) + b"\x48\x89\x10"
                + mov_rdx_imm64(mask[8:16]) + b"\x48\x89\x50\x08"
                + mov_rdx_imm64(mask[16:24]) + b"\x48\x89\x50\x10"
                + mov_rdx_imm64(mask[24:32]) + b"\x90\x90")
        with tempfile.TemporaryDirectory() as d:
            p = build_pe(Path(d), [(".text", code + b"\x90" * 256, True)])
            masks = extract_xor_masks(p, loose=True)
            self.assertIn(mask, masks)

    def test_missing_file(self):
        self.assertEqual(extract_xor_masks(Path("/nonexistent/x.dll")), [])


class TestSqlCipherPageCheck(unittest.TestCase):
    """构造一页真的 SQLCipher 4 页，验证校验判据本身是对的。"""

    @staticmethod
    def make_page(password: bytes) -> bytes:
        salt = bytes(range(16))
        # 页布局：salt(16) + [数据(4000)+IV(16)] + HMAC(64) = 4096，
        # HMAC 覆盖范围正是 salt 之后到 HMAC 之前的 4016 字节
        body = os.urandom(4000) + b"\x00" * 16
        enc_key = hashlib.pbkdf2_hmac("sha512", password, salt, KDF_ITER, KEY_SIZE)
        mac_salt = bytes(b ^ 0x3A for b in salt)
        mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, KEY_SIZE)
        mac = hmac.new(mac_key, body + struct.pack("<I", 1), hashlib.sha512).digest()
        return salt + body + mac

    def test_correct_password_passes(self):
        pw = os.urandom(32)
        page = self.make_page(pw)
        self.assertEqual(len(page), 4096)
        self.assertTrue(_derive_and_check(pw, page))

    def test_wrong_password_fails(self):
        pw = os.urandom(32)
        page = self.make_page(pw)
        self.assertFalse(_derive_and_check(os.urandom(32), page))

    def test_xor_roundtrip(self):
        """内存候选 XOR 掩码 == 真口令，这正是真机验证过的链路。"""
        pw = os.urandom(32)
        mask = os.urandom(32)
        raw = bytes(a ^ b for a, b in zip(pw, mask))
        recovered = bytes(a ^ b for a, b in zip(raw, mask))
        self.assertEqual(recovered, pw)
        self.assertTrue(_derive_and_check(recovered, self.make_page(pw)))


class TestFirstPage(unittest.TestCase):
    def test_short_file_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "tiny.db"
            p.write_bytes(b"x" * 100)
            with self.assertRaises(KeyFinderError):
                read_db_first_page(p)


if __name__ == "__main__":
    unittest.main()
