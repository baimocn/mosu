# -*- coding: utf-8 -*-
"""
墨溯 core —— 全库解密器
用自有 SQLCipher4 实现（core/sqlcipher4.py）把微信加密库解密为明文 SQLite 库，
再用标准 SQLite 打开。全程零 dll、零第三方授权检查。

用法:
    python core/decrypt_db.py <加密db路径> <password_hex> [输出明文db路径]
"""
import os, sys, struct, hashlib
from pathlib import Path
from Crypto.Cipher import AES
from sqlcipher4 import (PAGE_SIZE, RESERVE, DATA_END, IV_OFF, HMAC_OFF,
                        KDF_ITER, MAGIC, derive_enc_key)


def decrypt_file(src: str, password: bytes, dst: str = None) -> str:
    if dst is None:
        base = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(os.path.dirname(dst_dir(src)), base + "_plain.db")
    with open(src, "rb") as f:
        raw = f.read()
    if len(raw) % PAGE_SIZE != 0:
        print(f"  [!] 文件大小 {len(raw)} 不是 {PAGE_SIZE} 的整数倍，按页截断")
    npages = len(raw) // PAGE_SIZE
    salt = raw[:16]
    enc_key = derive_enc_key(password, salt)
    out = bytearray()
    for i in range(npages):
        page = raw[i * PAGE_SIZE:(i + 1) * PAGE_SIZE]
        pgno = i + 1
        iv = page[IV_OFF:HMAC_OFF]
        if pgno == 1:
            ct = page[16:DATA_END]
            pt = MAGIC + AES.new(enc_key, AES.MODE_CBC, iv).decrypt(ct)
            out += pt + b"\x00" * (PAGE_SIZE - len(pt))
        else:
            ct = page[:DATA_END]
            pt = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(ct)
            out += pt + b"\x00" * (PAGE_SIZE - len(pt))
    with open(dst, "wb") as f:
        f.write(bytes(out))
    return dst


def dst_dir(src):
    """解密产物目录：项目根下的 .cache（与 dataset 保持一致）。"""
    return str(Path(__file__).resolve().parent.parent / ".cache")


if __name__ == "__main__":
    import sqlite3
    src = sys.argv[1]
    pwd = bytes.fromhex(sys.argv[2])
    dst = sys.argv[3] if len(sys.argv) > 3 else None
    os.makedirs(dst_dir(src), exist_ok=True)
    if dst is None:
        base = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(dst_dir(src), base + "_plain.db")
    out = decrypt_file(src, pwd, dst)
    print(f"[✓] 解密完成 -> {out}  ({os.path.getsize(out)/1024:.0f} KB)")
    con = sqlite3.connect(out)
    con.row_factory = sqlite3.Row
    tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    print(f"[i] 表({len(tabs)}): {', '.join(tabs[:14])}")
    for t in tabs:
        try:
            n = con.execute(f"SELECT count(*) FROM '{t}'").fetchone()[0]
            print(f"    {t}: {n} 行")
        except Exception as e:
            print(f"    {t}: 读取失败 {str(e)[:40]}")
    con.close()
