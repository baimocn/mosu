# -*- coding: utf-8 -*-
"""
墨溯 core —— SQLCipher 4 解密核心（微信 4.1.x 参数）
自有实现，仅依赖 pycryptodome（开源 AES），无任何第三方授权检查。

页布局（page_size=4096, reserve=80）：
    [0:16]    salt
    [16:4016] 加密数据
    [4016:4032] IV
    [4032:4096] HMAC-SHA512(64B)
密钥派生：enc_key = PBKDF2-HMAC-SHA512(password, salt, 256000, 32)
HMAC 校验（SQLCipher 4 标准）：
    mac_salt = salt XOR 0x3a
    mac_key  = PBKDF2-HMAC-SHA512(enc_key, mac_salt, 2, 32)
    valid    = HMAC-SHA512(mac_key, page1[16:4032] + pack('<I',1)) == page1[4032:4096]
"""
import hashlib
import os
import re
import struct
from Crypto.Cipher import AES

PAGE_SIZE = 4096
RESERVE = 80
DATA_END = PAGE_SIZE - RESERVE          # 4016
IV_OFF = DATA_END                        # 4016
HMAC_OFF = IV_OFF + 16                   # 4032
KDF_ITER = 256000
MAGIC = b"SQLite format 3\x00"


def derive_enc_key(password: bytes, salt: bytes) -> bytes:
    """password(32B) + salt -> enc_key(32B)"""
    return hashlib.pbkdf2_hmac("sha512", password, salt, KDF_ITER, dklen=32)


def verify_password(password: bytes, db_path: str) -> tuple:
    """用数据库第一页做 HMAC 权威校验。返回 (是否有效, 详情)"""
    with open(db_path, "rb") as f:
        page1 = f.read(PAGE_SIZE)
    if len(page1) < PAGE_SIZE:
        return False, "文件不足一页"
    salt = page1[:16]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    enc_key = derive_enc_key(password, salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
    expect = page1[HMAC_OFF:HMAC_OFF + 64]
    actual = hashlib.pbkdf2_hmac  # 占位
    import hmac as _hmac
    actual = _hmac.new(mac_key, page1[16:HMAC_OFF] + struct.pack("<I", 1), hashlib.sha512).digest()
    return actual == expect, {"salt": salt.hex(), "enc_key": enc_key.hex(),
                              "expect": expect.hex(), "actual": actual.hex()}


def decrypt_page1(password: bytes, db_path: str) -> bytes:
    """解密第一页，返回完整 4096 字节明文页"""
    with open(db_path, "rb") as f:
        page1 = f.read(PAGE_SIZE)
    salt = page1[:16]
    iv = page1[IV_OFF:HMAC_OFF]
    ct = page1[16:DATA_END]
    enc_key = derive_enc_key(password, salt)
    pt = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(ct)
    return MAGIC + pt + b"\x00" * (PAGE_SIZE - 16 - len(pt))


def decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    """解密任意页（WAL 帧页同样适用）"""
    iv = page_data[IV_OFF:HMAC_OFF]
    if pgno == 1:
        ct = page_data[16:DATA_END]
        return MAGIC + AES.new(enc_key, AES.MODE_CBC, iv).decrypt(ct) + b"\x00" * RESERVE
    ct = page_data[:DATA_END]
    return AES.new(enc_key, AES.MODE_CBC, iv).decrypt(ct) + b"\x00" * RESERVE


if __name__ == "__main__":
    # 独立自测：python core/sqlcipher4.py <session.db 路径> [64位hex密钥]
    # 密钥也可以走环境变量 MOSU_KEY_HEX；两者都不给则只打印页面布局常量。
    import sys
    if len(sys.argv) < 2:
        print(f"PAGE_SIZE={PAGE_SIZE} RESERVE={RESERVE} DATA_END={DATA_END} "
              f"IV_OFF={IV_OFF} HMAC_OFF={HMAC_OFF}")
        sys.exit(0)
    db = sys.argv[1]
    key_hex = (sys.argv[2] if len(sys.argv) > 2
               else os.environ.get("MOSU_KEY_HEX", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key_hex):
        sys.exit("[x] 需要 64 位 hex 密钥（参数或 MOSU_KEY_HEX）")
    key = bytes.fromhex(key_hex)
    ok, info = verify_password(key, db)
    print("HMAC 校验:", "✅ 通过" if ok else "❌ 失败")
    print("  salt   =", info["salt"])
    print("  enc_key=", info["enc_key"])
    if ok:
        p1 = decrypt_page1(key, db)
        print("  解密页头:", p1[:32])
        print("  page_size 字段:", struct.unpack(">H", p1[16:18])[0])
    else:
        print("  expect =", info["expect"][:32], "...")
        print("  actual =", info["actual"][:32], "...")
