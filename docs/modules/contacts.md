# 联系人

通讯录名单 + 单个联系人的往来明细。这一页回答两个问题：**我有哪些往来对象**，
以及**我和某个人之间到底发生了什么**。

## 类型词表

`Dataset.contact_type(wxid)` 的判定顺序：

| 条件 | 返回 |
|------|------|
| wxid 以 `@chatroom` 结尾 | `group` |
| wxid 以 `gh_` / `wxid_gh` 开头 | `mp` |
| 在通讯录 contact 表里，`delete_flag` 为 0 | `friend` |
| 在通讯录里，`delete_flag` 非 0 | `former_friend` |
| 都不满足 | `other` |

> ⚠️ 这与 `/api/sessions` 的 `type` **不是同一套词表**：后者用 `_session_type()`，
> 值域只有 `private | group | mp`，永远不会返回 `friend` / `former_friend` / `other`。
两套判定还不一致：`_session_type()` 只认 `gh_` 前缀，`contact_type()` 额外认
`wxid_gh` 前缀——同一个公众号在两处可能得到不同类型。
> 前端共用同一个中文映射表掩盖了这个差异。两套词表统一是遗留项。

`former_friend` 的文案是「已删除（保留聊天记录）」，但判定**只看 `delete_flag`**，
不检查是否真有消息往来——一个删掉后从未聊过的人也会被这样标注。文案与判定存在偏差。

## 接口

### `GET /api/contacts`

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `q` | str | — | 子串匹配 `remark` / `nickname` / `wxid`（均 lower） |
| `type` | str | — | 按 `contact_type()` 过滤 |
| `all` | `1` | 缺省为过滤 | 设为 `1` 才包含「备注与昵称皆空」的条目 |
| `sort` | `messages` / `name` | `messages` | 按消息量降序 / 按名称 |
| `offset` | int | 0 | |
| `limit` | int | 100 | 上限 500 |

```json
{"contacts": [{"wxid": "...", "displayName": "AI coding", "type": "group",
               "messageCount": 149369, "remark": "", "nickname": ""}],
 "total": 3742, "hasMore": true}
```

**搜索不匹配 `alias`（微信号）**，但详情页会展示它——「搜得到但搜不到」的观感来自
这里。前端 placeholder 写「搜索备注/昵称/微信 ID」，与实现一致。

### `GET /api/contact?wxid=`

返回：

```json
{
  "wxid": "...", "displayName": "...",
  "type": "friend", "remark": "...", "nickname": "...", "alias": "...",
  "sessions": [{
    "sessionId": "...", "displayName": "...", "type": "private",
    "messageCount": 1467, "lastTime": 1762000000,
    "stats": {
      "session": {"sessionId": "...", "displayName": "...", "type": "private"},
      "totalMessages": 1467, "textMessages": 1137, "mediaMessages": 330,
      "byType": {"text": 1137, "image": 180, "emoji": 98},
      "sentMessages": 740, "receivedMessages": 727,
      "activeDays": 75,
      "firstMessageTime": 1773909480, "firstMessageText": "2026-03-19 11:58",
      "lastMessageTime": 1762000000,  "lastMessageText": "2026-10-18 18:40"
    }
  }],
  "groupTop":    [{"senderId": "...", "displayName": "...", "messageCount": 3000, "isSelf": false}],
  "groupTotal":  149369
}
```

三个必须知道的结构要点：

1. **往来统计不在顶层**，而在 `sessions[].stats` 里。同一联系人若存在多个会话，
   每个会话各自带一份统计；详情页需要跨会话合并。
2. `groupTop` **恒存在**（好友返回 `[]`），但为空数组；`groupTotal` **仅群聊有**，
   且仅在「type 是 group 且 sessions 非空」且 `group_stats` 成功时写入，失败时该键
   整个缺失。前端用 `detail.groupTotal || 0` 兜住。
3. `sessions[].stats` 在会话找不到时为 `null`。

**wxid 不在通讯录时**的兜底：先做子串匹配，**只在唯一命中时**才采用命中项；
多个命中则按原样返回 `{wxid}`，交给前端显示「数据库里没有你们的消息记录」。
（早期版本取第一个命中，会展示另一个人的数据。）

## 前端呈现

`gui/assets/views-data.js` 的 `Contacts` 组件，左 380px 名单 + 右侧详情。

| 情况 | 右侧展示 |
|------|---------|
| 未选中 | 空态：选择一位联系人，说明右侧会展示什么 |
| 好友/私聊 | 「往来统计」卡：总数、我发出（含占比）、对方发出（含占比）、文本/媒体、活跃天数、首条与最近一条 |
| 多会话 | 追加「会话列表」卡，逐会话列出条数与最近日期 |
| 群聊 | 「群活跃度」（消息总数 + 上榜成员数）+「成员发言排行」前 15（自己的发言标「我」） |
| 无消息记录 | 明确说明数据库里没有往来消息，而非空白 |

未覆盖字段（除已呈现的 9 个键外）进「其他记录」兜底卡片。深链 `#/contacts?wxid=xxx`
可直接打开对应联系人，选中时也会把 hash 写回地址栏。

## 性能

`/api/contacts` 需要遍历整张通讯录（3742 行）并逐条 `contact_type()` 判定，约
百毫秒级。`/api/contact` 对**每个**会话各跑一次 `session_stats()`，单会话通常
两条聚合 SQL，耗时可忽略；群聊额外一次 `group_stats()`。

两个端点都**全程持 `_ds_lock`**。

## 边界情况

| 场景 | 表现 |
|------|------|
| `contact.db` 缺失 | `contacts()` 返回 `{}` → `total: 0`、名单空。**前端无空态提示**，只显示「0 位」 |
| wxid 完全不存在 | 返回 `{wxid}`，详情页显示「数据库里没有你们的消息记录」 |
| 会话有记录但消息全在时间范围外 | 同上 |
| 群聊 `group_stats` 失败 | `groupTop`/`groupTotal` 键缺失，前端 `|| 0` 兜底 |

## 测试

`contact_type()` 的全分支（含 `former_friend` 边界）、`/api/contact` 的子串兜底唯一性
（见 `tests/test_insight_invariants.py`）已有覆盖。

`/api/contacts` 的 `q` 匹配字段、`all=1` 语义、`sort` 两个取值，以及
`sessions[].stats` 的合并逻辑都无测试。