# 墨溯 CLI 命令速查

入口：`python cli/mosu.py <命令>`（Windows 本机 Python：`C:\Python314\python.exe`）

## 全局选项

| 选项 | 说明 |
| --- | --- |
| `--db-path <dir>` | 微信账号目录（含 `db_storage`），默认自动定位 |
| `--key <hex>` | 手动指定 64 位数据库密钥 |
| `--wxid <名>` | 临时指定账号（多账号场景，见 `account`） |
| `--format <fmt>` | `json` / `jsonl` / `table` / `csv` / `markdown`，默认 table |
| `--limit <n>` | 条数限制，默认 20 |
| `--quiet` | 不打印解密等过程日志 |
| `--refresh` | 忽略缓存，重新快照并解密 |
| `--with-wal` | 尝试合并 WAL 帧（实验性，默认关） |

全局选项既可以放在子命令前，也可以放在子命令后（`mosu sessions --limit 10`）。

> 机器/AI 消费请用 `--format json`：字段名为英文（`sessionId` / `displayName` / `messageCount` …）；
> 人看的 table/csv/markdown 用中文表头。

## 命令

```bash
# 运行状态：配置、密钥 HMAC 校验、会话/联系人数量
mosu status

# 会话列表（按最后活跃排序）
mosu sessions --limit 20 [--type private|group|mp] [--offset N]

# 会话消息（默认取最新 N 条，按时间正序展示）
mosu messages "张三" --limit 50
mosu messages "张三" --type image --limit 10      # text|image|voice|video|file|emoji|system
mosu messages "张三" --direction out --limit 20    # in|out
mosu messages "张三" --from 2026-01-01 --to 2026-06-30
mosu messages "张三" --order asc --limit 20        # 从最早开始
mosu messages "张三" --cursor <sortSeq>            # 游标翻页

# 联系人
mosu contacts --limit 20                             # 默认按消息量排序
mosu contacts --type group
mosu contacts info 张三

# 密钥
mosu key get [--full]        # 显示当前密钥（默认脱敏）
mosu key test                # 用 session.db 首页做 HMAC 校验
mosu key set <64位hex>       # 写入 ~/.mosu/config.json 并校验
mosu key migrate             # 把旧版密钥库迁到 ~/.mosu

# 多账号
mosu account list | current
mosu account add <名> --db-path <目录> --key <hex> [--img-key <hex>] [--use]
mosu account use <名>        # 设默认账号
mosu account remove <名>
mosu <任意命令> --wxid <名>  # 临时指定账号，不改默认

# 全文搜索（会逐条解压消息体，全库约 10-60 秒）
mosu search 场馆 --limit 20 [--session 张三] [--from ...] [--to ...]

# 统计
mosu stats global
mosu stats contacts --top 20
mosu stats time --by hour|weekday|month|date
mosu stats session "张三"
mosu stats keywords "张三" --top 30
mosu stats group "UU pudding"

# 朋友圈
mosu moments --limit 20 [--user wxid_xxx] [--from ...] [--to ...]

# 年度报告
mosu report --year 2026
mosu report --all-time
mosu report --session 张三

# 导出
mosu export "张三"                                   # HTML（含图片/视频/文件），默认 ./export/
mosu export "张三" --output D:\导出\张三
mosu export "张三" --with-media                       # 额外做语音转码（SILK→MP3，较慢）
mosu export "张三" --export-format json|csv|txt
mosu export --all --output D:\导出 [--max-sessions 20]  # 全量，按会话名建子目录

# 画像蒸馏（不给会话名=全库个人画像）
mosu profile                              # 全库：作息/社交图谱/话题/风格/月度/侧写
mosu profile 张三                        # 单联系人：互动/发起回复/话题对比/情感/信号/时间线
mosu profile 张三 --format json          # 结构化输出（给 AI/脚本）
mosu profile --from 2026-01-01 --max-msgs 20000

# 配置
mosu init --db-path <dir> --key <hex>
mosu config show
mosu config set --db-path <dir>

# MCP Server（stdio，给 AI 客户端用）
mosu mcp serve            # 起服务，日志走 stderr，协议走 stdin/stdout
mosu mcp selftest         # 不启服务，把每个工具跑一遍
mosu mcp config           # 打印接入用的 mcp.json 片段
```

## MCP Server

`core/mcp_server.py` 是一个零依赖（只用标准库 + 现有 core）的 MCP 服务，
把上面所有能力暴露成 **11 个 tools**，供 Claude Code / WorkBuddy 等客户端直接调用。

| 工具 | 作用 |
|---|---|
| `mosu_status` | 数据源状态：账号目录、本机 wxid、会话数、联系人数 |
| `mosu_sessions` | 会话列表（可按名字过滤） |
| `mosu_messages` | 会话消息（`type`/`direction`/`from`/`to`/`order`，默认最新 20 条） |
| `mosu_contacts` / `mosu_contact_info` | 联系人列表与详情 |
| `mosu_search` | 全库关键词搜索 |
| `mosu_profile` | global / contacts / time / session / keywords / group |
| `mosu_moments` | 朋友圈 |
| `mosu_profile` | 画像蒸馏（全库/单联系人，`markdown=true` 直接给报告） |
| `mosu_report` | 年度 / 全时间 / 单会话报告 |
| `mosu_export` | 导出 html / json / csv / txt |
| `mosu_media` | 按 imgMd5 解密本地图片；`as=base64` 时**直接把图回传**，AI 能看图 |

设计要点：

- **日志只能走 stderr**，stdout 是协议通道，任何 print 都会打断握手。
- 消息返回前会瘦身（丢无用字段、长文本截断 400 字），`limit` 上限 200，防止撑爆上下文。
- 数据源懒加载：接到第一个真实 tool 调用才解密建索引，避免握手阶段卡住客户端。
- 结果同时给 `content[0].text`（紧凑 JSON）与 `structuredContent`。

接入配置（已写入 `~/.workbuddy/mcp.json` 的 `mcpServers.mosu`）：

```json
{
  "mosu": {
    "type": "stdio",
    "command": "C:/Python314/python.exe",
    "args": ["D:/Desktop/墨溯/core/mcp_server.py"],
    "env": { "PYTHONIOENCODING": "utf-8" }
  }
}
```

## 实现要点

- 解密：`core/sqlcipher4.py` 自有 SQLCipher4（reserve=80、PBKDF2-HMAC-SHA512×256000），零 dll。
- 快照：每次先复制 db 再解密；微信边写边拷会损坏快照，故解密后做 schema 校验，失败自动重试一次，仍失败则跳过该库并告警。
- 分片合并：微信把旧消息留在 `message_1.db` 等分片，`sessions/messages/search/stats` 均跨分片合并并按 `(create_time, local_type, content)` 去重。
- 方向判定：`real_sender_id == 本机 Name2Id.rowid` → out；本机 wxid 由账号目录名推出（`wxid_xxx_e027` → `wxid_xxx`）。
- WAL：微信 4.1 的 WAL 帧 salt 与主库不一致（WCDB 私有机制），帧解密后 quick_check 失败，故默认关闭 `--with-wal`。
