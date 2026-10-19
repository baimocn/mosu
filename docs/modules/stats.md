# 数据统计

全库与单会话的消息计数、构成、时间分布与高频词。这个模块只做**计数与聚合**，
不读消息正文做语义分析（那是[画像](profile.md)的职责），因此它是四个洞察模块里
唯一能在秒级返回的。

## 数据来源与分片

微信 4.x 把消息按时间切进多个库：`message/message_0.db`、`message_1.db`……
同一个会话的表 `Msg_<md5(username)>` 会**同时存在于多个分片**。索引构建期已按
会话合并并保留分片列表（`Dataset._shard_list`）。

`Analytics._shards()` 因此做「所有会话 × 所有分片」的展开，聚合全部在 Python 内存
里完成，不做跨库 SQL：

| 聚合项 | 合并方式 |
|--------|----------|
| 计数 | 逐分片 `count(*)` 相加，Counter 累加 |
| 时间边界 | 逐分片 `min/max` 折叠 |
| 联系人榜 | 先按分片出行，再按 `wxid` 二次求和 |

**已知口径差异**：统计走 `count(*)` 不去重，而消息读取路径 `raw_messages()` 用
`_dedup_key`（`create_time + local_type + content`）显式去重。当同一分片里存在重复行时，
本页总数会**高于**浏览页看到的条数。这是已知未对齐项，尚未修（见主文档「遗留风险」）。

消息类型先经 `KIND_BY_TYPE` 由 `local_type` 映射为 `text/image/voice/video/emoji/
file/system`，未命中一律 `other`。注意 `T_APPMSG(49)` 被整体映射为 `file`——链接、
引用、小程序、转账都并进了「文件」。

## 接口

### `GET /api/stats/global`

无入参。本机全量统计。

```json
{
  "totalMessages": 232808, "totalSessions": 190, "totalContacts": 3742,
  "textMessages": 137000, "mediaMessages": 95000,
  "byType": {"text": 137000, "file": 25400, "image": 25200, "emoji": 4400,
             "system": 4365, "voice": 1200, "video": 793, "other": 450},
  "timeRange": {"first": 1748700000, "last": 1762000000,
                "firstText": "2025-06-01", "lastText": "2026-10-20"}
}
```

`byType` 的键集合固定为上述 7 种，**不会出现** `link`。若前端出现 `link` 映射即为
幻觉类型（审查发现过一次）。

### `GET /api/stats/contacts`

| 参数 | 类型 | 默认 | 上限 |
|------|------|------|------|
| `top` | int | 20 | 100 |

```json
{"contacts": [{"wxid": "...", "displayName": "AI coding: The future is here",
               "type": "group", "messageCount": 149369}],
 "total": 190}
```

### `GET /api/stats/time`

| 参数 | 取值 | 默认 |
|------|------|------|
| `by` | `hour` / `weekday` / `month` / `date` | `hour` |

四种取值的键格式**不同**，接入时务必按下表处理：

| `by` | `distribution` 键 | `items[0]` 示例 |
|-----|-------------------|----------------|
| `hour` | `"00"`–`"23"`（**字符串，带前导零**） | `{"bucket": "00", "count": 9789}` |
| `weekday` | `"0"`–`"6"`（`%w`，0=周日） | `{"bucket": "周日", "key": "0", "count": 27789}` |
| `month` | `"2026-05"` | `{"bucket": "2025-06", "count": 2035}` |
| `date` | `"2026-05-25"` | `{"bucket": "2025-06-15", "count": 172}` |

只有 `weekday` 的 `items` 带 `key`（原始 `%w` 值），其余三种 `key` 缺失，前端用
`it.key || it.bucket` 兼容。**只返回实际出现的桶，不补零**——凌晨无消息的小时在图上
直接缺失而非显示 0。

非法 `by` 抛 `ValueError` → HTTP 400。

### `POST /api/stats/keywords`

| 参数 | 类型 | 约束 |
|------|------|------|
| `top` | int | 1–100，越界 400 |
| `max_msgs` | int | 1000–200000，越界 400 |

```json
{"taskId": "a1b2c3", "status": "running"}
```

**字段名是 `taskId`**。前端必须读 `d.taskId`；读 `d.id` 会得到 `undefined`，任务照跑
但 UI 永远空白——这是审查发现过的真实静默失效。

轮询 `GET /api/task?id=<taskId>`，返回 `{id, kind, status, cost, result, error}`，
`status ∈ {queued, running, done, error}`。前端轮询上限 300 × 1.5s = 450s。

结果结构：`{"keywords": [{"word": str, "count": int}], "scannedMessages": int, "cost": float}`。

### 错误码

统一由 `_guard` 转换（`gui/server.py`）：

| 异常 | 状态码 |
|------|--------|
| `SystemExit` / `LookupError` / `KeyError` | 404 |
| `ValueError` | 400 |
| 其他 | 500（消息带 `类型: 详情`） |

## 前端呈现

`gui/assets/views-insight.js` 的 `Stats` 组件，五块内容自上而下：

1. **四个磁贴**——消息总数（`分布 N 个会话`）、联系人（`通讯录全部条目，含群聊与
   公众号`）、最活跃的一天、时间跨度。磁贴三行分工固定：标题=指标名、值=数字、
   脚注=口径说明。
2. **活跃度日历**——最近一年逐日热力图，颜色分 6 档（0 档留白），格子按**周日起步**
   排列（`endDay = end.getDay()`），月份标签只在含 1 号的那列出现，避免重叠。
3. **消息构成 + 聊得最多的会话**——并排两栏，各带口径说明。
4. **什么时间在聊**——`hour`/`weekday`/`month` 三档切换。
5. **全库高频词**——字号映射频次，点击后走异步任务。

**口径差异提示**：`timeRange` 为空时 `first/last/firstText/lastText` 都是 `null`，
前端用 `|| '—'` 兜底。但 `g` 非空即渲染，空库会显示一屏 0 而非空态。

**加载顺序**：首屏用 `Promise.all` 并发拉三个端点，但 `_ds_lock` 是全局 `RLock`，
三者实际串行。热力图（`by=date`，需全表 `GROUP BY`）单独异步加载，不拖首屏，
代价是「最活跃的一天」卡片首屏会先闪一下 `—`。

## 性能

| 操作 | 23 万条量级 | 备注 |
|------|-------------|------|
| `global_stats` | 0.3–2s | 每分片 2 条聚合 SQL；无索引，成本随会话数线性放大 |
| `time_stats(hour)` | < 0.5s | 单次 `GROUP BY strftime` |
| `time_stats(date)` | 0.3–1s | 同上，但结果集大一个量级 |
| `keyword_stats` | 20–60s | 逐条解压 + jieba 分词，默认扫 20000 条 |

`keyword_stats` 是唯一逐条解码消息的统计，走 `TaskHub` 异步（单 worker 串行）。
但 worker 内部**整段持 `_ds_lock`**，期间所有数据 API 阻塞。`max_msgs` 允许到 200000，
该规模下持锁数分钟——这是已知的体验风险。

## 测试

`tests/test_insight_invariants.py` 固化了三条契约：`weekday` 桶名与
`WEEKDAY_NAMES` 逐日对齐、`hour` 用字符串键、类型映射不含幻觉类型。

`global_stats` / `contact_stats` / `time_stats` / `keyword_stats` 本身目前**无单测**
（真实数据依赖太重），四个端点也无 HTTP 层测试。优先补测项：`time_stats` 四种
`by` 的键格式快照、keywords 的 `taskId` 契约、跨分片重复计数场景。