# 年报

以「自然年」为单位的年度汇总。与[画像](profile.md)相比，它更轻：没有逐条解压消息，
除了关键词那一项；结构也只有一层 `summary`，因此可以用固定版式渲染而不必兜底。

## 三种口径

`Analytics.report(year=None, all_time=False, session=None)` 的分支优先级是
**`all_time` > `session` > `year`**：

| 调用 | 时间范围 | `scope` | `year` 返回值 |
|------|---------|---------|--------------|
| `report()` | 当前自然年 1/1 → 次年 1/1 | `"year"` | `year or 今年` |
| `report(year=2025)` | 2025-01-01 → 2026-01-01 | `"year"` | `2025` |
| `report(all_time=True)` | 无限制 | `"all"` | **`null`（传入的 year 被静默丢弃）** |
| `report(session="X")` | 无限制 | `"session"` | 原样回显但**不生效** |

> 两个入口的默认值不同：GUI 的 `/api/report` 不带 `year` 时走 `all_time=True`，
> 所以网页上的「全部年份」= 全库；CLI `mosu report` 不带参数则是「本年」。CLI 与
> 网页对同一句话的理解不一样，接入时务必显式传参。

## 返回结构

```json
{
  "scope": "year",
  "year": 2025,
  "sessionId": null,
  "summary": {
    "totalMessages": 232808,
    "sentMessages": 19270,
    "receivedMessages": 213524,
    "activeDays": 475,
    "topContacts": [{"wxid": "...", "displayName": "...", "count": 149369}],
    "topKeywords": [{"word": "项目", "count": 1204}],
    "hourlyDistribution": {"00": 1203, "01": 800, "...": 0},
    "firstMessageTime": 1735660800,
    "lastMessageTime": 1767196800
  },
  "meta": {"scannedMessages": 232808, "cost": 8.2}
}
```

要点：

- `topContacts` 含**群聊**（按会话统计，不是按联系人）
- `hourlyDistribution` 的键是零填充字符串 `"00"`–`"23"`，与其他模块一致
- `firstMessageTime` / `lastMessageTime` 是 **epoch 秒**，不是格式化字符串
- 首末消息的**正文**没有出现在返回里（代码算出过但未返回，见「遗留风险」）

## 接口

### `GET /api/report`

| 参数 | 类型 | 不给时的行为 |
|------|------|--------------|
| `year` | int | 走 `all_time=True`，即全库合计 |

GUI 年份下拉提供「全部年份」+ 最近 8 年。

## 前端呈现

`views-insight.js` 的 `ReportView` 组件——年报是四个模块里唯一有**专属版式**的，
不走 `JsonView`，因为它的返回结构足够稳定：

1. **四个磁贴**：总消息（脚注「2025 年」/「全部年份合计」）、我发出（脚注占比）、
   对方发出（脚注占比）、有消息的天数（脚注首末日期）。
2. **最常联系**：前 10 个会话横向条形图。
3. **时段分布**：24 小时横向条形图。后端键是字符串 `"00"`–`"23"`，
   前端 `parseInt` 后按数值排序。
4. **这一年常说什么**：词云式 chip，字号映射频次，取前 40。

顶部同样有 meta 脚注（扫描条数 / 用时 / 全部本机计算）。

## 性能

全库口径约 **5–15 秒**：内含 `global_stats`（2N 查询）+ `contact_stats`（N）+
发送方向循环（N）+ 活跃天数去重（N 次 `strftime DISTINCT`）+ `time_stats`（N 次
`GROUP BY strftime`）+ `keyword_stats`（30000 条解压），约 6N 次全表扫。

走**同步 GET**，全程持 `_ds_lock`。页面有骨架屏但**无进度、无取消**。

## 测试

`report()` 目前无单测，`/api/report` 无 HTTP 层测试。最该补的是口径矩阵：
`{}` / `{all_time}` / `{year}` / `{session, year}` 四组输入断言 `scope` 与时间范围——
现有实现的「year 在 all_time 分支被静默丢弃」正是这类测试能提前暴露的。