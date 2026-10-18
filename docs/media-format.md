# 墨溯 · 微信 4.1 媒体格式手册（实测 4.1.15.13）

全部结论均在本机实测验证，非推测。配套代码：`core/msgparse.py`、`core/media.py`、`core/voice.py`。

---

## 一、消息体：zstd + protobuf，不再是裸 XML

`Msg_<md5(username)>` 表关键列：

| 列 | 说明 |
|---|---|
| `local_type` | **高位是 flag**，真实类型 = `local_type & 0xFFFFFFFF` |
| `message_content` | zstd 压缩（魔数 `28 B5 2F FD`），解压后是 XML；纯文本消息则是明文 |
| `packed_info_data` | protobuf，**媒体本地定位靠它** |
| `real_sender_id` | = `Name2Id` 表的 **rowid**（不是 wxid） |
| `create_time` | 秒级时间戳，也是关联语音的键 |

真实类型常量：1 文本 / 3 图片 / 34 语音 / 43 视频 / 47 表情 / 49 分享文件 / 10000 系统。

> 踩坑：不 mask 高位的话，会看到 `25769803825` 这种怪值（真实是 49）。

### packed_info_data 结构

```
图片: field3 { field4: <32位 hex md5> }        -> 本地文件名 md5
视频: field4 { field8: <32位 ASCII id> }       -> 视频/封面文件名
语音: field5 { field2: <UTF-8 转文字> }        -> 语音识别文本
```

**关键**：XML `<img md5="...">` 里的 md5 是**原图 md5**，跟本地文件名**对不上**（命中率 0%）。
必须用 packed_info_data 里的 md5（命中率 **99.8%**）。

---

## 二、图片

### 存放

```
msg/attach/<dir_md5>/<YYYY-MM>/Img/<file_md5>_W.dat   大图
msg/attach/<dir_md5>/<YYYY-MM>/Img/<file_md5>_t.dat   缩略图
```

`dir_md5` 常为对方 wxid 的 md5（`wxid_xxxx` → `40bf6896...`）。
实测本机：15183 个 md5、425 个 Img 目录。

### 三代加密（按文件头判断）

| 版本 | 头 | 方案 |
|---|---|---|
| V0 | 无魔数 | 整文件单字节 **XOR 0x3D** |
| V1 | `07 08 56 31 08 07` | AES-128-ECB 段 + XOR 段（固定 key） |
| V2 | `07 08 56 32 08 07` | AES-128-ECB 段 + XOR 段（**账号级 key**） |

V2 精确布局：

```
[0:6]    魔数 07 08 56 32 08 07
[6:10]   aes_size  (LE u32)
[10:14]  xor_size  (LE u32)
[14]     标志 (恒 0x01)
[15 : 15+aes_size]        AES-128-ECB 密文（图像头部）
[15+aes_size : +16]       分隔段（不解密，跳过）
[15+aes_size+16 :]        XOR 单字节加密的剩余部分（xor_size 字节）
校验: 文件大小 == 15 + aes_size + 16 + xor_size      ← 实测精确成立
```

XOR key = **0x3D**（可由文件尾 `FF D9` 反推验证）。

### V2 的 AES key：已攻克（2026-10-18）

**性质**（实测 2000 个文件统计结果，这些判据是破案关键）：

- `aes_size` 恒为 **1024**，`flag` 恒为 **0x01**
- 分隔段 16 字节对所有文件**完全相同**（账号级常量，不是 IV）
- **ECB 而非 CBC**：AES 段内部存在重复密文块（实测 8/64），CBC 下不可能重复
- key 是 **AES-128**，账号级，全库通用（实测随机抽 300 个 V2 文件，215 个解出真实
  图像；失败的是非图片附件）
- key 形态是 **16 字节 ASCII 字符串**（不是随机二进制），所以任何"从 db key 派生"
  的思路都不可能命中 —— 这两把 key 完全独立

**获取方法（可复现）**：

1. 微信 4.1 **不常驻**该 key，平时扫内存扫不到
2. **必须在微信里打开一张图**（图片查看器窗口，标题"图片和视频"），此时 key 必然
   被加载进内存
3. 立刻转储进程内存（`scripts/dump_live.py`，主进程约 780MB，20 秒）
4. 全量逐字节扫，靶 = 第一密文块解密后前 8 字节 == 标准 JFIF 头
   `ff d8 ff e0 00 10 4a 46`（`scripts/scan_img_key.py`，32 进程约 43 分钟）
5. **必须用 PIL 真打开做终审** —— 只比对 JPEG 魔数的初筛在 7.8 亿候选下必出假阳性

已排除的死路（别再走）：XML 里的 `aeskey`（那是 CDN 传输用，0/24 命中）、
db key 及其派生产物（123 个候选 0 命中）、注册表、磁盘配置文件、明文库全扫、
5 个微信进程内存、明文图片附近 ±4MB。

**持久化**：key 写入密钥库 `~/.mosu/config.json` 的 `imgKeyHex` 字段（与 db key 同库），
`core/media.py` 按 `MOSU_IMG_KEY_HEX` 环境变量 → `.cache/img_aes_key.txt` →
`~/.mosu/config.json` 回退读取。

> 缩略图解密后即可直接查看；大图若是 WxAM 私有压缩则还需二次解压（本机样本暂未遇到）。

---

## 三、语音（重要：不在磁盘上）

**语音不落文件**，存在 `db_storage/message/media_0.db` 的 `VoiceInfo` 表：

```
VoiceInfo(chat_name_id, create_time, local_id, svr_id, voice_data, data_index)
voice_data = SILK_V3 字节流（含 "#!SILK_V3" 标记）
```

- 关联方式：`VoiceInfo.create_time == 消息.create_time`（同一秒）
- 本机实测：2748 条语音记录，覆盖约 34 个会话
- 转码链：**silk-wasm(Node) 解码 → PCM s16le → ffmpeg → mp3**（ffmpeg 本身不认 silk）
- 采样率 **24000 Hz**（解码时长与消息 `voicelength` 核对一致）

`packed_info_data` 里还能拿到微信的**语音转文字**，无音频时也能显示文本。

---

## 四、视频 / 文件

```
msg/video/<YYYY-MM>/<video_id>.mp4          本体（明文）
msg/video/<YYYY-MM>/<video_id>_thumb.jpg    封面
msg/file/<YYYY-MM>/<原文件名>                接收的文件（明文）
```

`video_id` 来自 packed_info_data 的 field4.field8（32 位 ASCII）。

---

## 五、发送者身份

- `real_sender_id` = `Name2Id.rowid`
- 本机自己 = rowid **1**（`wxid_xxxx`）
- 显示名取 `contact.remark`（备注）优先，其次 `nick_name`
