# 墨溯 mosu

> 本地微信数据解密与导出工具 —— 自有 SQLCipher 4 引擎，零 DLL、零网络、零授权检查。

一句话：把微信电脑端那些打不开的 `.db` 文件，变成你能读、能搜、能统计、能导出的数据。

```bash
pip install -r requirements.txt
python cli/mosu.py init --db-path "<微信数据目录>" --key <64位hex>
python cli/mosu.py sessions
```

---

## 为什么做这个

微信电脑版把聊天数据存在 SQLCipher 4 加密的 SQLite 里，密钥在它自己进程的内存里。
市面上的现成工具要么是闭源黑盒、要么带授权锁、要么直接调用官方 DLL ——
作者不干了、锁触发了、或者你只是想在自己的脚本里读一条消息，就只能干瞪眼。

墨溯把这套链路**完整自研**：

| 层 | 做法 |
|---|---|
| 加密层 | 自有 SQLCipher 4 实现（PBKDF2-HMAC-SHA512 × 256000，AES-256-CBC + HMAC-SHA512） |
| 解析层 | zstd 解压 + protobuf + XML，消息/联系人/群/语音/视频/朋友圈全格式 |
| 媒体层 | 图片 V0（XOR）与 V2（私有容器，AES-128-ECB + 尾部 XOR）解密 |
| 应用层 | CLI 命令集 + MCP Server，五种输出格式 |

加密层用的是**开源社区公开的参数**（不重造密码学），其余全部自有代码。
**没有任何 DLL 注入、没有进程操作、没有网络请求。** 纯本地、纯只读。

## 特性

- **全库解密**：`session` / `contact` / `message` / `media` / `sns`，多分片自动合并去重
- **消息查询**：按会话、类型（文本/图片/语音/视频/文件/表情）、方向、时间段、游标翻页
- **全文搜索**：逐条解压后匹配，搜得到压缩消息体里的词
- **统计**：全局概览、联系榜、小时/星期/月分布、单人统计、关键词、群成员榜
- **画像蒸馏**：全库个人画像 + 单联系人深度画像（作息/社交图谱/话题/情感曲线/亲密度信号）
- **个人 bot 素材**：本人发言语料包（jsonl + 按会话分段）+ 风格画像
- **数字分身服务**（`mosu bot`）：云端 LLM（OpenAI 兼容，任意后端可换）+ RAG 从你的历史发言检索风格参考 + 规则硬校验（AI 腔禁词/长度/感叹号/照抄检测，不达标自动带反馈重生成）
- **微信通道**（`mosu wechat`）：微信官方 Bot API（iLink）自研实现——扫码登录、长轮询收发、游标持久化、context_token 管理、常驻计划任务。收到消息 → 墨溯用你的风格生成回复 → 原路发回。不依赖任何第三方框架
- **本地 GUI 控制台**（`mosu gui`）：浏览器打开 `127.0.0.1:8788`——对话调试分身、微信通道状态灯、消息流水、数据浏览（会话列表 + 微信气泡消息流 + 图片解密直出 + 全库搜索）。零构建链、零 Node 运行时
- **导出**：HTML（微信气泡式，图片视频内嵌）/ JSON / CSV / TXT，可全量
- **朋友圈**：正文、图片、点赞、评论
- **MCP Server**：12 个工具，AI 客户端可直接调用，图片能直接"看"
- **离线情感分析**：内置中文情感词典 + 否定/程度规则，不调任何外部 API

## 安装

```bash
git clone <repo> && cd mosu
pip install -r requirements.txt
```

- Python >= 3.10（标准库 + 4 个 PyPI 包）
- `jieba` 可选：没有它关键词统计退化为 2-gram
- `Pillow` 可选：没有它导出图片不缩放
- Node.js：仅"语音 SILK → MP3"转码需要
- ffmpeg：可选，导出语音为 mp3 时需要

测试（全离线，37 例）：

```bash
python -m unittest discover -s tests -v
```

## 快速开始

### 1. 配置

墨溯的配置放在自己的 `~/.mosu/config.json`（环境变量 `MOSU_HOME` 可改位置）：

```bash
# 命令行方式
python cli/mosu.py init --db-path "~/Documents/xwechat_files/wxid_xxxx" --key <64位hex>

# 或不落盘，全程环境变量
export MOSU_DB_PATH="~/Documents/xwechat_files/wxid_xxxx"
export MOSU_KEY_HEX=<64位hex>
export MOSU_IMG_KEY_HEX=<32位hex>     # 可选，V2 加密图片需要
```

| 环境变量 | 作用 |
|---|---|
| `MOSU_HOME` | 配置目录，默认 `~/.mosu` |
| `MOSU_DB_PATH` | 微信账号目录（含 `db_storage` 的那一层） |
| `MOSU_KEY_HEX` | 64 位 hex 数据库密钥 |
| `MOSU_IMG_KEY_HEX` | 32 位 hex 图片 AES 密钥 |

**密钥从哪来**：数据库密钥是微信 SQLCipher 的 64 位 hex 口令，只能从**本机微信进程内存**
或你此前用其他工具导出的结果里取得（社区有公开的只读内存扫描实现）。
墨溯本身不注入、不 hook、不联网，只做纯本地解密。
图片密钥同理，且需先在微信里打开过一张图，该 key 才会驻留内存。

密钥管理：`mosu key get | test | set <hex>`。

**多账号**（一台机器登过多个微信）：配置里存多个账号，随时切换。

```bash
python cli/mosu.py account add wxid_xxxx --db-path "<目录>" --key <hex> [--img-key <hex>]
python cli/mosu.py account list          # 列出全部账号（标注当前）
python cli/mosu.py account use wxid_xxxx # 设默认账号
python cli/mosu.py account remove wxid_xxxx
python cli/mosu.py sessions --wxid wxid_xxxx   # 临时指定，不改默认
```

旧的单账号配置会在首次读取时自动升级成多账号格式，无需手动迁移。
账号配了目录但目录不存在时会明确报错，不会静默读到别的账号数据。

### 2. 用起来

```bash
python cli/mosu.py status                          # 配置 + 密钥校验 + 数据规模
python cli/mosu.py sessions --limit 20             # 会话列表
python cli/mosu.py messages "张三" --limit 50    # 读消息
python cli/mosu.py search 关键词 --limit 20        # 全库搜索
python cli/mosu.py stats global                    # 全局统计
python cli/mosu.py profile                         # 全库个人画像
python cli/mosu.py profile "张三"                # 单联系人深度画像
python cli/mosu.py persona --output persona/     # 个人 bot 素材：语料包 + 风格画像
python cli/mosu.py bot test "在吗"               # 数字分身试一句（先配好 key）
python cli/mosu.py bot serve                     # 起服务：POST 127.0.0.1:8787/chat
python cli/mosu.py gui                           # 本地控制台：浏览器打开 127.0.0.1:8788
python cli/mosu.py gui install                  # 常驻：登录即启动 + 崩溃自愈
python cli/mosu.py wechat login                  # 微信通道扫码绑定（一个 bot 只能一个消费者）
python cli/mosu.py wechat install                # 常驻：登录即启动 + 崩溃自愈
python cli/mosu.py wechat status                 # 通道状态
python cli/mosu.py moments --limit 10              # 朋友圈
python cli/mosu.py export "张三" --output out/   # HTML 导出（含图片/语音/视频）
python cli/mosu.py export "张三" --incremental    # 之后只导新消息（秒级）
```

完整命令与选项见 [docs/CLI.md](docs/CLI.md)。

### 3. 输出格式

`--format json | jsonl | table | csv | markdown` —— JSON 用英文字段名，给脚本/AI 消费；
表格用中文表头，给人看。

## MCP Server

墨溯可以作为一个 MCP server 跑起来，让 AI 客户端直接调用：

```bash
python cli/mosu.py mcp serve          # stdio，日志走 stderr
python cli/mosu.py mcp selftest       # 不起服务，逐个工具冒烟
python cli/mosu.py mcp config         # 打印接入用的配置片段
```

12 个工具：`mosu_status` / `mosu_sessions` / `mosu_messages` / `mosu_contacts` /
`mosu_contact_info` / `mosu_search` / `mosu_stats` / `mosu_moments` /
`mosu_report` / `mosu_export` / `mosu_profile` / `mosu_media`。

其中 `mosu_media(md5, as=base64)` 能把解密后的图片直接回传 —— AI 可以"看"聊天里的图。

接入示例（`mcp.json`）：

```json
{
  "mosu": {
    "type": "stdio",
    "command": "python",
    "args": ["/path/to/mosu/core/mcp_server.py"],
    "env": { "PYTHONIOENCODING": "utf-8" }
  }
}
```

## 隐私与合规

- **只读**：从不写入、修改、删除微信的任何文件；解密产物落在项目 `.cache/`，可随时清
- **不联网**：没有任何网络请求代码；导出 HTML 里的图片是本地拷贝，不引用外链
- **不注入**：不操作微信进程，不做 DLL 注入、不做 hook、不做内存扫描
- **你自己负责钥匙**：密钥由你提供并保管，墨溯只是用它做本地解密
- **仓库本身是干净的**：代码里不含任何密钥、个人信息或硬编码路径；
  配置只在你本机的 `~/.mosu/config.json`，导出物在 `.cache/`，两者都已 gitignore

**免责声明**：本工具面向**个人对自己数据**的备份、分析与迁移。请勿用于未经授权的
数据访问，也不要将导出的他人隐私内容公开发布。使用即表示你同意自行承担合规责任。

## 项目结构

```
cli/mosu.py            CLI 入口（13 组命令）
core/
  sqlcipher4.py        SQLCipher 4 解密核心（页布局/密钥派生/HMAC 校验）
  dataset.py           统一数据层：定位账号 → 快照 → 解密 → 跨分片索引 → 查询
  msgparse.py          消息体解析（zstd + protobuf + XML）
  media.py             图片/视频/文件索引与 V0/V2 解密
  voice.py             语音 SILK → PCM/MP3
  exporter.py          HTML 气泡渲染
  analytics.py         统计与年度报告
  profile.py           画像蒸馏
  sentiment.py         中文情感打分（词典 + 否定/程度规则）
  sns.py               朋友圈
  formatters.py        json/jsonl/table/csv/markdown 输出
  mcp_server.py        MCP Server（stdio，12 工具）
  task.py              常驻：Windows 计划任务（首选）/ 启动文件夹+VBS 守护（降级）
gui/
  server.py            GUI 控制台 HTTP 服务（/api/chat、/api/status、/api/log）
  index.html           单页前端（Vue3 本地 vendor，离线可用）
  vendor/              Vue 3 运行时（随仓库分发，不依赖 CDN）
tests/                 离线测试（83 例）
docs/                  命令速查、媒体格式、SQLCipher 参数说明
```

## 路线图

- [x] 核心解密引擎（SQLCipher 4 自有实现）
- [x] 消息/联系人/群/语音/视频/朋友圈全格式解析
- [x] CLI 命令集 + 五种输出格式
- [x] MCP Server（12 工具）
- [x] 画像蒸馏（个人画像 + 单联系人深度画像）
- [x] 离线测试套件（37 例）
- [x] 增量导出（按最后导出时间续跑）
- [x] 多账号切换（account 命令组 + `--wxid` 临时指定）
- [x] 数字分身服务（bot：云端 LLM + RAG 风格锚点 + 规则硬校验）
- [x] 微信通道（wechat：iLink 自研，扫码登录/长轮询/常驻）
- [x] 本地 GUI 控制台（gui：对话调试 + 通道状态 + 消息流水 + 数据浏览）
- [x] GUI 数据统计（小时/星期/月分布、联系榜、关键词，自绘 SVG）
- [x] GUI 朋友圈（正文/图片/点赞/评论，offset 分页）
- [x] GUI 导出（HTML zip / JSON / CSV / TXT，异步任务 + 防目录穿越）
- [ ] Windows / macOS 一键打包
- [ ] GUI 扩展：统计图表类型扩充、语音播放

## 已知限制

- **WAL 实时合并不了**：微信 4.1 的 WAL 帧 salt 与主库不一致（其私有机制），
  最后几秒内的实时消息可能读不到
- **语音**：只有微信确实下载了音频的会话才有真语音，其余仅转写文字
- **图片密钥**：V2 加密图片需要账号级 AES key，该 key 不常驻内存，
  需先在微信里打开过一张图再提取

## English

**mosu** decrypts and exports your local WeChat (PC) chat data with a
from-scratch SQLCipher 4 engine — no DLL injection, no network, no license check.

```bash
pip install -r requirements.txt
export MOSU_DB_PATH="/path/to/xwechat_files/wxid_xxx"
export MOSU_KEY_HEX=<64-hex-key>
python cli/mosu.py sessions
python cli/mosu.py profile            # personal data portrait
```

See [docs/CLI.md](docs/CLI.md) for all commands. The database key can only be
obtained from your own machine's WeChat process memory or a previous export —
this tool never touches the WeChat process itself.

## License

[MIT](LICENSE) © 2026 傅铭胜 and contributors
