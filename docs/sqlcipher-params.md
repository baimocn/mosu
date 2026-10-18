# 微信 4.1.x 数据库加密参数（实测确认）

> 墨溯 M1 核心成果 —— 2026-10-17 在微信 **4.1.15.13** 上实测通过（HMAC 权威校验）。
> 数据目录：`<微信数据目录>\xwechat_files\wxid_xxxx\db_storage`

## 一、算法参数

| 项 | 值 |
|---|---|
| 加密 | SQLCipher 4：AES-256-CBC + HMAC-SHA512 |
| 密钥派生 | PBKDF2-HMAC-SHA512，`iter=256000`，`dklen=32` |
| 输入 | password（32 字节，**账号级**）+ 每个库独立的 salt |
| page_size | 4096 |
| reserve | **80**（不是 48 —— 这是最容易踩的坑） |

## 二、页布局（page_size=4096）

```
[0:16]      salt
[16:4016]   加密数据（4000 字节）
[4016:4032] IV（16 字节）
[4032:4096] HMAC-SHA512（64 字节）
```

- 第 1 页特殊：解密后明文 = `b"SQLite format 3\x00"` + AES-CBC(密文[16:4016])
- 其他页：明文 = AES-CBC(密文[0:4016])

## 三、HMAC 校验（判定密钥是否正确的权威方法）

```python
mac_salt = bytes(b ^ 0x3A for b in salt)
enc_key  = pbkdf2_hmac('sha512', password, salt, 256000, 32)
mac_key  = pbkdf2_hmac('sha512', enc_key, mac_salt, 2, 32)
valid    = hmac.new(mac_key, page1[16:4032] + pack('<I', 1), sha512).digest() == page1[4032:4096]
```

## 四、常见误区（本次踩过的坑，勿重犯）

1. **reserve 不是 48**：SQLCipher 3 习惯值，微信 4.1 用 80（HMAC 64B + IV 16B）。用 48 会导致 150+ 参数组合全部失败
2. **内存里的是 password（32B）不是已派生 enc_key**：4.1+ 不再缓存 `x'<hex>'` 字符串，需自行 KDF 派生
3. **微信 4.0.3.36+ / 4.1 起不再长期把密钥留在内存**：老的内存扫描思路失效；但**同一大版本内密钥不变**
4. **跨大版本会换密钥**：4.0 → 4.1 升级会重新加密数据库，旧密钥作废（本例旧密钥属 4.0 时代，实测在 4.1.15.13 上仍有效，说明未跨版本重加密）
5. `better-sqlite3-multiple-ciphers` 的 `kdf_algorithm` / `hmac_algorithm` pragma 要整数枚举，字符串会报 `Malformed integer value`

## 五、自有实现位置

- `core/sqlcipher4.py` —— 参数常量 + 密钥派生 + HMAC 校验 + 单页解密
- `core/decrypt_db.py` —— 全库解密为明文 SQLite（可再用标准 sqlite3 打开）

## 六、实测结果

| 库 | 结果 |
|---|---|
| session/session.db | 420KB，`SessionTable` 277 行、Name2Id 245 |
| contact/contact.db | 3.4MB，contact 3741 行、chat_room 44 |
| message/message_0.db | 108MB，163 个 `Msg_<md5(sessionId)>` 表、Name2Id 1253 |

## 七、WAL 处理（待办）

微信库带 `-wal` 文件，帧格式：`pgno(4,大端) + unk(4) + salt(16) + page(4096)`；
解密用**主库 enc_key**，IV = 帧页[4016:4032]，加密范围 [0:4016]。帧内 salt 仅用于密钥轮换检测。
