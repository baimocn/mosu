# 画像

把聊天记录蒸馏成对「我」（本人画像）或「TA」（联系人画像）的描述。这个模块与
[数据统计](stats.md)的根本差别：**它会逐条解压消息、做分词与情感打分**，因此慢一个
数量级，字段也更接近语义而非计数。

## 两种画像

`core/profile.py` 的 `ProfileDistiller` 提供两个入口，返回结构完全不同：

| | 本人画像 | 联系人画像 |
|---|---------|-----------|
| 入口 | `self_profile()` | `contact_profile(query)` |
| 顶层标识 | `scope: "self"` | `scope: "contact"` |
| HTTP | `/api/profile`（无参数） | `/api/profile?session=<会话名或 wxid>` |

`scope` 只用于渲染分发（`render_markdown` 选渲染器），不参与业务判断。

两者**没有共用的子结构**：`rhythm` 在本人画像里是 `{hourly:[{hour,total,mine}×24],
weekday:[{day,total,mine}×7], peakHour, peakWeekday, quietest4hStart,
nightMessages, morningMessages}`，在联系人画像里是 `{hourlyMine:[int×24],
hourlyTheirs:[int×24], monthly:[{month,count}]}`。前端能同时渲染是因为走了通用
`JsonView`，代价是**无法做任何共用组件**。

## 返回字段

### 本人画像 `self_profile(from_ts, to_ts, max_msgs=60000, top=15, per_session=300)`

| 顶层键 | 二级字段 |
|--------|---------|
| `meta` | `scannedMessages`（SQL 全量）、`textSampled`（采样条数）、`myMessages`、`cost` |
| `overview` | `totalMessages` `myMessages` `myShare` `firstText` `lastText` `activeDays` `spanDays`(int) |
| `rhythm` | `hourly[24]` `weekday[7]` `peakHour` `peakWeekday` `quietest4hStart` `nightMessages` `morningMessages` |
| `social` | `[{wxid, name, mine, total, share}]`，按 `mine` 降序取前 15 |
| `topics` | `byTopic[{topic,hits,ratio}]` `keywords[{word,count}]` `bigrams[{word,count}]` |
| `style` | `avgLength` `byKind{类型:int}` `emojiRate` `questionRate` |
| `monthly` | `[{month:"YYYY-MM", mine, total}]` |
| `insights` | `[str]`，见下 |

### 联系人画像 `contact_profile(query, from_ts, to_ts, top=10)`

| 顶层键 | 二级字段 |
|--------|---------|
| `session` | `sessionId` `displayName` `type` |
| `meta` | `scannedMessages` `cost`（**无 `textSampled`**） |
| `overview` | `totalMessages` `myMessages` `theirMessages` `myShare` `activeDays` `firstText` `lastText` `spanDays`(**float**) |
| `rhythm` | `hourlyMine[24]` `hourlyTheirs[24]` `monthly[{month,count}]` |
| `interaction` | `initiatedByMe` `initiatedByThem` `myAvgReplySeconds` `theirAvgReplySeconds` |
| `topics` | `mine` `theirs`（`[{topic,hits,ratio}]`）、`keywordsMine` `keywordsTheirs` |
| `sentiment` | `mine` `theirs`（各含 `avg` `positive` `negative` `neutral` `positiveRate` `topPositiveWords` `topNegativeWords`）、`curve[{bucket,avg,count}]` |
| `signals` | `emojiRate` `laughPer100` `lengthTrend` `mediaShare` `voiceMine/Theirs` `imageMine/Theirs` |
| `milestones` | `firstMessage` `lastMessage` `{time,who,content}`、`firstVoice/Image/Video` `{time,who}`、`busiestDay{day,count,sample}` |
| `timeline` | `[{month, count, firstText, peakDay{day,count,sample}}]` |

`spanDays` 一处 `int`（本人）一处 `float`（联系人，`round(...,1)`），属于已知的类型
不一致。

## 计算口径

### 两段式数据来源（重要）

画像的数字**混用两种口径**，页面上只靠 `meta` 一个数字提示：

- `overview` / `rhythm` / `social` / `monthly` 来自 `_sql_aggregate()` 的**全量 SQL**
- `topics` / `style.avgLength` / `questionRate` / `insights` 来自 `_collect()` 的
  **采样文本**

采样规则：按会话分层，最近活跃的会话优先，每个会话最多取**最近** `per_session=300`
条，整体上限 `max_msgs=60000`。

> 采样方向曾出过一次严重错误：`iter_session_rows` 默认 `ORDER BY sort_seq`（升序），
> 上游数到 300 就 `break`，实际取到的是每个会话**最早**的 300 条。14 万条的大群因此
> 只有开头参与话题与风格统计。现已通过 `iter_session_rows(desc=True, limit=N)` 修正，
> 跨分片取回后按 `create_time` 合并再截断。

### 关键指标定义

| 指标 | 定义 |
|------|------|
| `initiatedByMe/Them` | 相邻消息间隔 **> 6 小时**视为新一轮对话，统计这一条是谁发的 |
| `myAvgReplySeconds` | 方向切换的相邻往返间隔；**> 12 小时的不计入** |
| `myShare` | 我发出 / 总条数 |
| `nightMessages` | 0–5 点本人消息数 |
| `morningMessages` | 6–9 点本人消息数（与夜间不重叠） |
| `quietest4hStart` | 24 小时中消息最少的连续 4 小时窗口的起点；**候选起点只取 0–20**，会漏掉 21/22/23 三个跨零点窗口 |
| `sentiment.avg` | 中文情感词典逐条打分（−1~1）后平均；单条 ±0.15 以内记中性 |
| `lengthTrend` | 后半程平均消息长度 vs 前半程：> 1.15 倍记「变长」，< 0.85 倍记「变短」，否则「持平」 |

### `insights` 的触发规则（最多产出 8 条）

`insights` 由 `_self_insights()` 按固定规则生成，**非 AI 生成、可复现**：

| 条件 | 输出 |
|------|------|
| 夜间消息占比 > 8% | 夜猫子作息 |
| 平均消息长度 < 15 字 | 短句派 |
| 平均消息长度 > 40 字（与上一条互斥） | 长文型 |
| 带问号消息占比 > 12% | 提问型沟通 |
| 表情消息占比 > 8% | 表情包高频 |
| 话题前三含「AI/编程」 | 技术/开发者身份显著 |
| 话题前三含「生意/接单」 | 经营线并行 |
| 话题前三含「考研/学业」 | 学业线存在 |
| 存在峰值星期 | 「周X 是最活跃的一天」 |

两处刻意的约束：

- **星期名查表不硬编码**（`WEEKDAY_NAMES`），且该表按 SQLite `%w` 定义——`0=周日`。
  曾硬编码成「周五/周六」而 `%w` 的 4/5 实为周四/周五。
- **不再声称「周末高于工作日」**。那个结论需要比较周末与工作日两组总量，代码从未
  计算过，现在只陈述已计算的峰值星期。

### 联系人画像的「我 / 对方」二分

群聊沿用「我 / 对方」二分口径：14 万条、数百成员的群会被聚合成「对方」。这是已知
的口径局限，见「遗留风险」。

## 接口

### `GET /api/profile`

| 参数 | 说明 |
|------|------|
| `session` | 可选。给出则生成该联系人画像，不给则生成本人画像 |

会话名解析走 `Dataset.find_session()`，支持会话显示名或 wxid。查不到抛
`SystemExit` → HTTP 404，GUI 显示「生成失败」。

### `POST /api/persona`

异步生成 bot 语料包（全库扫描，1–2 分钟），走 `TaskHub`。与画像统计无关。

## 前端呈现

`views-insight.js` 的 `Insight` 组件。核心机制是**分区配置驱动**：

```js
PROFILE_SECTIONS.self = [
  ["insights", "结论速览", "由本页各项统计按固定规则归纳，非 AI 生成，可复现。"],
  ["overview", "总览"],
  ["style", "表达风格"],
  ...
];
PROFILE_SECTIONS.contact = [
  ["overview", "总览"],
  ["interaction", "互动质量", "「开启对话」指间隔超过 6 小时的第一条消息由谁发出…"],
  ...
];
```

- 只渲染后端**实际返回**了的分区，顺序按配置
- `meta` / `scope` / `session` 列入 `PROFILE_HIDDEN` 不进卡片；`meta` 提到页首做脚注
- 未在配置里出现的键进「补充信息」兜底卡片
- **数值人类化**：`*share` `*Rate` `ratio` → 百分比；`*Seconds` → 分钟/小时；
  `avgLength` → 带「字」；空值字段整行不渲染
- 顶部显示「扫描 N 条消息 · 抽样 N 条文本 · 用时 N 秒 · 全部本机计算」

## 性能

`self_profile` 在 23 万条量级约 **6–10 秒**（SQL 聚合 2–5s + 采样解压分词 4–6s），
由 `meta.cost` 如实上报。

`contact_profile` **没有耗时上限**：内部 `raw_messages(limit=10**9)` 会把整个会话读入
内存并做两次情感打分。对 14 万条的大群可能耗时以分钟计、内存数百 MB。已知风险。

两者都在**整个执行期间持 `_ds_lock`**，期间所有数据 API 阻塞。GUI 是
`ThreadingHTTPServer`，页面不会冻结（有骨架屏），但没有进度条也没有取消入口。

## 测试

`tests/test_insight_invariants.py` 固化：星期映射、`insights` 不做未计算的推断、
夜间窗口文案口径、采样倒序能力、`social` 用 wxid 作键、markdown 空库不崩。

`core/profile.py` 整体仍无功能单测（真实数据依赖重）。优先补测：`_self_insights`
阈值边界、`contact_profile` 空会话 → `SystemExit`、`render_markdown` 空库。