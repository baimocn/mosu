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
本页总数会**高于**浏览页看到的条数。

这一差异现在**写进了返回值**：`global_stats` 与 `session_stats` 都带
`"deduped": false`，前端磁贴脚注「按数据库原始行统计，跨分片重复行未去重」。
写死 `false` 是诚实的——真做了 SQL 侧去重再改 `true`。全表 `DISTINCT` 的代价与收益
不成比例，本轮不做。

消息类型先经 `KIND_BY_TYPE` 由 `local_type` 映射为 `text/image/voice/video/emoji/
file/system`，未命中一律 `other`。注意 `T_APPMSG(49)` 被整体映射为 `file`——链接、
引用、小程序、转账都并进了「文件」。

## 接口

### `GET /api/stats/global`

| 参数 | 类型 | 说明 |
|------|------|------|
| `from` | 时间串 | 可选。`2026-01-01` / `2026-01-01 12:30` / unix 秒，走 `core.dataset.parse_time` |
| `to` | 时间串 | 同上 |

两个都缺省＝全量，**既有行为不变**。

```json
{
  "totalMessages": 232808, "deduped": false,
  "totalSessions": 190, "totalContacts": 3742,
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
| `from` / `to` | 时间串 | — | 可选，缺省全量 |

```json
{"contacts": [{"wxid": "...", "displayName": "AI coding: The future is here",
               "type": "group", "messageCount": 149369}],
 "total": 190}
```

### `GET /api/stats/time`

| 参数 | 取值 | HTTP 默认 | 函数默认 |
|------|------|-----------|----------|
| `by` | `hour` / `weekday` / `month` / `date` | `hour` | **`month`** |
| `from` / `to` | 时间串 | — | 可选，缺省全量；与 `by` 正交 |

> 两层默认值不同：直接调 `Analytics.time_stats()` 不传 `by` 拿到的是**按月**，
> 走 HTTP 才拿到按小时。

四种取值的键格式**不同**，接入时务必按下表处理：

| `by` | `distribution` 键 | `items[0]` 示例 | 桶数 |
|-----|-------------------|----------------|------|
| `hour` | `"00"`–`"23"`（**字符串，带前导零**） | `{"bucket": "00", "count": 9789}` | **恒 24** |
| `weekday` | `"0"`–`"6"`（`%w`，0=周日） | `{"bucket": "周日", "key": "0", "count": 27789}` | **恒 7** |
| `month` | `"2026-05"` | `{"bucket": "2025-06", "count": 2035}` | 出现过的月份数 |
| `date` | `"2026-05-25"` | `{"bucket": "2025-06-15", "count": 172}` | 出现过的日期数 |

只有 `weekday` 的 `items` 带 `key`（原始 `%w` 值），其余三种 `key` 缺失，前端用
`it.key || it.bucket` 兼容。

**`hour` / `weekday` 补零，`month` / `date` 不补。**

补零的理由：只返回出现过的桶，柱状图会**在视觉上撒谎**——凌晨没有消息的那个小时
干脆不画，看起来像「数据缺失」而不是「真的没有」。这两种维度长度固定（24/7），
补零零成本。

不补的理由：`month` / `date` 补出来的空月份是**噪声不是信息**——跨年查询会拖出
十几个 0，读者要自己分辨「那年没数据」和「那年真的一条都没有」。

补零只影响展示，`total` 仍是真实消息数（补零桶 count 为 0，不进累加）。

非法 `by` 抛 `ValueError` → HTTP 400。

### `POST /api/stats/keywords`

| 参数 | 类型 | 默认 | 约束 |
|------|------|------|------|
| `top` | int | 30 | 1–100，越界 400 |
| `max_msgs` | int | 20000 | 1000–**20000**，越界 400 |

> 注意 `top=0` 会被 `or 30` 吞掉变成 30，**不触发 400**。

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
但 worker 内部**整段持 `_ds_lock`**，期间所有数据 API 阻塞。两道缓解：

1. **服务端硬上限收到 20000**（与默认值一致）。原先允许到 200000，那个规模下
   持锁数分钟且**没有任何超时能救**——包括用户点「取消」。
2. **`Analytics._texts()` 每 5000 条 `sleep(0)` 让出一次 GIL**。逐条解压是纯
   Python 计算，不主动让出的话即使跑在工作线程里也会饿死持锁的主线程。
   `sleep(0)` 不改变任何统计结果，只是把控制权交回调度器。

## 测试

`tests/test_insight_invariants.py` 固化了：`weekday` 桶名与 `WEEKDAY_NAMES`
逐日对齐、`insights` 不做未计算的推断、采样倒序能力、`social` 用 wxid 作键、
markdown 空库不崩、`/api/contact` 子串兜底唯一性。

`tests/test_analytics_guards.py` 覆盖聚合口径本身（用内存 SQLite 造真表跑真 SQL，
不是 mock）：`hour` 恒 24 桶 / `weekday` 恒 7 桶 / `month`·`date` 不补、补零不抬高
`total`、`deduped` 标志、`report` 首末消息、`contact_profile` 上限与 `truncated`、
两处 `spanDays` 同为 float、`max_msgs` 上限、统计端点 from/to 接线。