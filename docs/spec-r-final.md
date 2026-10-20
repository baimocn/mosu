# 墨溯 · 收尾批次规格定稿（产品设计）

蓝本清｜基线 `933c2c4`｜基线 154 例全绿
⚠️ 跑测试须用 `C:\Python314\python.exe -m unittest discover -s tests -q`；默认 `python` 缺 jieba，会假失败 14 例

## 0. 事实核实（与主理人复核结论一致，无冲突）

已采信 `.cache/v031-facts.md`，并逐条独立复核：MCP `TOOLS` 实测 12 条、README 5 处「12」全部正确；`store.py` grep `persona` 零命中（落盘上下文不记人格）；`theme.css:96/173` 数值、`shortcut.py:107/113` 无 `Icon=` 均一致。**D1–D4 定稿在此事实基础上给出。**

**另有一项事实文件未点透、但我复现出来的真缺陷**（见矩阵 #6）：`_session_type()` 只认 `gh_` 前缀，`contact_type()` 额外认 `wxid_gh`，导致 `wxid_gh*` 公众号在 `/api/sessions` 显示「单聊」、在 `/api/contacts` 显示「公众号」——**同一个账号在两个页面类型不同**。这不是词表映射问题，是判定分歧，本轮必须修。

---

## 1. D1–D4 定稿

### D1 默认人格改为倾听者

**定稿：`DEFAULT_PERSONA = "listener"`，上下文按人格分桶（不做过滤）。**
分桶而非过滤：过滤会让切人格后上下文清空，用户抱怨「AI 忘了刚才说的话」；分桶既保连续又杜绝串味。落点是人格并入 `conv_id`，零侵入（复用现成 `_fname`/`_loaded`）。

1. `core/bot/personas.py`：`DEFAULT_PERSONA`(L121)→`"listener"`；`SWITCH_HELP`(L142)「默认模仿」→「默认倾听者」；`PERSONAS` 顺序不动（listener 已首位）
2. `core/bot/service.py::chat`(L201)：归一 `cid = f"{conv_id or 'default'}::{p.id}"`，**同一变量**贯穿 `_history()` 与 `_record_history()`，下游不得重拼
3. `gui/server.py:1356` 的 `conv_id="gui"` 自动分桶；存量无 `::` 文件自成旧桶，不迁不删
4. **控制台首屏**：`views-console.js:249` 已是 `persona = d.current`，来自 `bot.persona_id`＝新默认 → **前端零改动**即高亮倾听者；`pickPersona`(L230) 现状不会被 `fetch_()` 覆盖，不动
5. **微信首次风格告知**（`core/wechat.py` L414 循环内）：`bot.chat()` 发送成功后，若进程内首次回复且 `persona_id==DEFAULT_PERSONA`，同批追加一条：
   > 我现在是「倾听者」：少说多听、接住情绪，不给建议。想换风格发 `/模仿`（你的口气）、`/分析`（用真实数据）、`/安慰`（先接情绪）。
   模块级 `_greeted` 门闩，**进程内只发一次**，不落盘；切换指令走 `continue` 不触发
6. **向导加一行**：`views-setup.js:23` 的 `card-sub` 末尾追加「AI 对话默认以『倾听者』开口，随时可切。」——**不新增 DOM**，向导 `steps` 不动

**验收**：`test_persona.py` 断言 `DEFAULT_PERSONA=="listener"`、`SWITCH_HELP` 含「默认倾听者」；新增 `test_context_bucketed_by_persona`：同一 `conv_id` 两人格各聊一次后，`_history("x::listener")` 与 `_history("x::mimic")` **长度各为 2 且互不可见**；磁盘出现两个含 `::` 的 json。

### D2 品牌绿统一

**定稿：logo 一字不动；把 `--brand` 令牌向 logo 色系靠拢，并压深到能扛白字。**

先说实测出的**既存缺陷**：当前浅色 `--grad-brand` 四色标对白字对比度 `3.47/2.60/2.02:1`，`--brand` 纯色 `3.47:1`——`.btn-primary` 用该渐变 + `--brand-text:#fff`，**全部不达 WCAG AA 4.5:1**。所以这不只是「两套绿」的美观问题，是既存可读性缺陷，本轮顺手修掉。

**不改 logo 的理由**：logo 是静态资产，渲成 PNG/ICO 后进 favicon／快捷方式／任务栏；三色标（`#1ac873/#07c160/#03a251`）是 CHANGELOG 明写的「微信绿同源」刻意设计，代表产品联想。改 logo 要重跑 Edge 无头渲染＋重生成 9 尺寸 ICO；反过来让令牌靠拢成本低得多。

**色值（WCAG 相对亮度实算）**

| 令牌 | 现状 | 改为 | 对比度 | 色相 |
|---|---|---|---|---|
| `--brand`（浅） | `#0e9e56` | **`#0b8548`** | 白字 **4.71:1** ✅ | 150.0（logo 系 148.7–150.7） |
| `--brand-hover`（浅） | `#0b8548` | **`#08693a`** | 6.79:1 | 150.9 |
| `--grad-brand`（浅） | `#0e9e56→#16b866→#34cf85→#0e9e56` | **`#0b8548→#12813f→#0d7c42→#0b8548`** | **4.71/4.96/5.28/4.71 全部 ✅** | 148.6–151 |
| `--border-focus`（浅） | `#0e9e56` | **`#0b8548`** | 对底 `#f4f6f9` 4.35:1（需≥3）✅ | — |
| 深色全部令牌 | — | **不动** | `--brand` 对 `#0d1117` 7.47:1 ✅ | — |

**落地**：`theme.css` 浅色块改上表 4 项 + `--brand-soft`(L98)/`--glow-brand`(L128) 的 rgba 同步为 `11,133,72` + `--heat-4`(L137)→`#0b8548`（`--heat-5` 保持 `#0d7c42`，两档 1.12:1 仍可辨）+ `--c1`(L141)→`#0b8548`；深色块不动。渐变去掉浅色高光档，「亮起来」交给 `app.css:172` 已有的 `--grad-sheen`，位移动画保留。`logo.svg`、`app.css`、`make_icon.py` 均不改。

**验收**：新增 `tests/test_brand.py`（内置 WCAG 亮度函数）断言浅 `--brand`+白字≥4.5、浅渐变**四标逐一**≥4.5、深 `--brand`+`#0d1117`≥4.5、焦点环≥3；并断言 `logo.svg` 仍含 `#07c160`（防误改）。

### D3 macOS / Linux 快捷方式设图标

**定稿：macOS 给 `.icns` 文件＋提示；Linux 用 `Icon=` 指向已安装绝对路径。三级降级，不硬失败。**

macOS 的 `.command` 在 Finder 侧**无法**绑图标（只有 `.app` bundle 支持），而 `.app` 是独立打包产物线（`Info.plist`+`CFBundleIconFile`+签名），不在本轮范围 → 只在桌面写一份 `墨溯.icns` 并提示可手拖。Linux 按 freedesktop 规范：优先绝对路径，回退主题名。

**落地**（`core/shortcut.py`）
1. 新增常量 `ICON_PNG = ROOT/"gui/assets/logo-512.png"`（**已存在，512×512 RGBA，已验证**）、`ICON_ICNS = .../mosu.icns`
2. 新增 `_resolve_icons()` 返回 `{"png":Path|None,"icns":Path|None}`，全缺则双 None
3. `_shortcut_posix()`：darwin 写完 `.command` 后 `shutil.copyfile` icns 到桌面（同名覆盖幂等）；linux 先 `mkdir -p ~/.local/share/icons/hicolor/512x512/apps` 并 copy 成 `mosu.png`，成功写绝对路径 `Icon=`，失败写 `Icon=mosu` 主题名回退，**PNG 不存在则完全不写 `Icon=` 键**（`.desktop` 仍合法，GNOME 显示默认图标）
4. 新增 `ensure_icns()`：有 Pillow 则 `Image.save(..., format="ICNS")`（**本机 Pillow 13.0.0 实测可写，产出 399KB**）；缺 Pillow 或缺 PNG 则打印 `[i]` 并 `return False`，**不抛异常**
5. `create_shortcut()`(L128)：posix 分支先 `ensure_icns()` 再建入口
6. `remove_shortcut()`(L142)：现非 Windows 直接 return → **扩到 mac/Linux**（删 `.command`/`.desktop`/桌面 icns；**不删** `~/.local/share/icons` 那份）

**降级验收**：有 Pillow+PNG → 生成 icns、`.desktop` 含绝对路径 Icon；无 Pillow 有 PNG → mac 只出 `.command`、**Linux 仍设 PNG 图标**，退出码 0；两者皆无 → 两平台均不设图标、退出码 0；Linux home 只读 → `Icon=mosu` 回退；重复执行不产生重复文件。新增 `tests/test_shortcut.py` 用 `MOSU_HOME` 指向 tmp 断言 Icon 行有无。

### D4 README 的 MCP 工具数

**定稿：README 数字本身无误、不动内容；只加一条测试锁住 `len(TOOLS)` 与文档一致，防止再次腐化。**

事实：README L46/203 写的「12 工具」是**正确的**（`TOOLS` 实测 12 条、`t_mosu_*` 处理器 12 个，一一对应无孤儿）。所以本轮**不改 README 任何字**——它没错。

但「写死数字必然腐化」这个风险是真的：本轮就出现过一次「12 vs 16」的误传（复核方数错）。**防腐化靠测试，不靠改文档措辞。**

**落地**：新增 `tests/test_docs.py::test_readme_mcp_tool_count_matches_registry`
- `from core.mcp_server import TOOLS`；`real = len(TOOLS)`
- 正则从 README 抓 `(\d+)\s*(?:个)?\s*工具` 与 `(\d+)\s*tools`，断言所有抓到的数字 **== `real`**
- 反向断言 `real == 12`（锁住基线，将来加工具必须同步改 README，测试会红提醒）
- 双向名称集合断言：README 中出现的 `mosu_[a-z_]+` 集合 == `TOOLS` 名字集合（既不许多写也不许漏写）

`CHANGELOG` **不要**写「工具数对齐」——那件事本来就没错，写进去反而造出「曾经 12 现在 16」的假历史。

---

## 2. 遗留风险处理矩阵

| # | 风险 | 处置 | 理由 |
|---|---|---|---|
| 1 | 统计 `count(*)` 不去重 vs 浏览页去重 | **本轮做（轻量）** | 会被当 bug 报。但正解（SQL 侧按 `create_time+local_type+content` 去重）要全表扫，代价失衡。**只做口径透明**：`global_stats/session_stats` 返回值加 `"deduped": false`，前端磁贴脚注「按数据库原始行统计，跨分片重复行未去重」。真去重列入**不做** |
| 2 | 统计接口不接受 `from_ts/to_ts` | **必须做** | core 层已全线贯通（`_where`），纯接线、零风险、零性能代价，且是「按时间看统计」的前提。改 `gui/server.py::api_stats_global/api_stats_contacts/api_stats_time` 各加 `from`/`to`，复用 `core.dataset.parse_time`（`api_messages:340` 已是此用法） |
| 3 | `contact_profile` 无耗时上限 | **必须做** | **唯一会打挂服务**的一条：`raw_messages(limit=10**9)`(`profile.py:320`) + 同步持锁(`server.py:805`) → 14 万条大群拖死 GUI 且**无任何超时能救**。定稿：`api_profile` 改走 TaskHub（复用 `tasks.submit`，与 `_kw_task` 同构）+ 加 `max_msgs=60000` 上限 + 超限标记 `summary.truncated=true`；前端画像页改 taskId 轮询（抄同文件 keywords 轮询） |
| 4 | `keyword_stats` 整段持锁 | **必须做** | 与 #3 同源但更易触发（`max_msgs` 上限 200000）。定稿：服务端硬上限收到 20000（与默认一致），并在 `_texts`(`analytics.py:270`) 内每 5000 条让出一次。不改数据结构 |
| 5 | 群画像「我/对方」二分 | **不做** | 这是**口径变更不是缺陷修复**：改成员级聚合＝新增「群成员画像」特性，牵动 `profile.py` 大半与整页叙述。已在 `docs/modules/profile.md:114` 明示为已知口径。**本轮只在 UI 加一句免责说明** |
| 6 | `contact_type`(5值)/`_session_type`(3值) 判定分歧 | **必须做（含后端一行）** | 我已复现真缺陷：`_session_type()` 只认 `gh_`，`contact_type()` 额外认 `wxid_gh` → **`wxid_gh*` 公众号在 `/api/sessions` 显示「单聊」、在 `/api/contacts` 显示「公众号」，同一账号两页类型不同**。且 `session_type=2` 时更糟：显示「群聊」。这不是映射问题，是判定分歧。定稿：①`core/dataset.py:669` `_session_type` 的 `gh_` 判断补上 `or username.startswith("wxid_gh")`（一行，与 `contact_type():511` 对齐）；②前端 `views-data.js:577` 拆 `contactTypeName`/`sessionTypeName` 两张表。**只合并「mp 前缀判定」这一条，不合并两套词表本身**——它们各有存在理由（通讯录看 `delete_flag`、会话看 `session_type`） |
| 7 | `report()` 首末消息正文死代码 | **必须做** | 已算出（`analytics.py:354-364`，两次 `raw_messages` 都执行了），扔掉纯浪费。放进 `summary.firstMessage/lastMessage`；前端在 session 口径下展示（非 session 为 `null`，`v-if` 隐藏）。`/api/profile` 同步调用，加字段不增耗时 |
| 8 | 空库显示一屏 0；`/api/contacts` 无空态 | **必须做** | 纯前端零后端风险，但决定「第一次打开」的印象——比报错更糟。`views-insight.js` Stats(L162) 在 `g.totalMessages===0` 时改渲染空态（复用同文件 L293 现成空态 DOM）；`views-data.js` 左栏(L415) 在 `!loading && !list.length` 插空态，文案分两种：完全没数据→「先在设置里配好密钥」；有库但筛选无果→「没有匹配的联系人」 |
| 9 | 时间分布不补零 | **本轮做** | 只返回出现过的桶 → 柱状图**在视觉上撒谎**：凌晨无消息的小时不显示柱，看起来像「数据缺失」而非「真的没有」。`time_stats`(L173) 在 `by=="hour"` 补齐 24 桶、`"weekday"` 补齐 7 桶；**只补这两种固定长度维度**（`month`/`date` 补零会产生大量空月份＝噪声）。前端无需改 |
| 10 | `MediaStore.files` 同名覆盖 | **不做** | 追完**无真实受害路径**：`self.files` 只在 `media.py:334` 的 `stats()` 里被 `len()` 报个数，**没有任何按 key 取文件的调用点**（图片走 `self.images` 按 md5、视频走 `videos`、缩略图走 `thumbs`，皆哈希索引不受影响）。同名覆盖只让「文件」统计数偏小。改动需重设缓存格式（`build()` 会把 `files` 写入 json 缓存 L248）。**改为在 `docs/modules/media.md` 记一句已知偏差** |
| 追加 | `spanDays` 本人画像 `int`、联系人画像 `float` | **必须做** | 原清单漏了（`docs/modules/README.md:51`）。一行 `float()` 统一；纯前端防御，但跨页比较时 `3 !== 3.0` 在 JS 为 `true`，会让分段判断错乱 |

---

## 3. 本轮不做清单

1. 统计口径真去重（#1 完整版）——全表 DISTINCT，代价失衡
2. 群成员级画像（#5）——独立特性，需单独设计群页叙述
3. macOS `.app` bundle 打包（D3）——独立产物线，只给 icns＋提示
4. 后端 `contact_type`/`_session_type` 词表合并（#6）——只拆前端映射
5. `MediaStore.files` 索引重构（#10）——只写文档
6. `mimic` 人格去留讨论——默认改了≠删功能，四人格保留
7. **任何新的 HTTP 端点**——只给既有接口加可选 query 参数（#2）
8. logo 重设计／品牌视觉升级——D2 只调令牌，logo 一字节不动
9. Windows 快捷方式改造——`.lnk`+`mosu.ico` 现状可用
10. 性能压测／基准测试工程——本轮只加单测断言

---

## 4. 前后端接口约定

### 4.1 变更的既有接口（无新增端点）

| 端点 | 变更 | 说明 |
|---|---|---|
| `GET /api/stats/global` | 新增 query `from`,`to` | ISO 日期串 → `core.dataset.parse_time`。**均可选，缺省＝全量**，既有行为不变 |
| `GET /api/stats/contacts` | 新增 query `from`,`to` | 同上 |
| `GET /api/stats/time` | 新增 query `from`,`to` | 同上，与既有 `by` 正交 |
| `GET /api/stats/global` | 新增字段 `deduped:false` | 常量；写死 false 是诚实的，将来真去重再改 true |
| `GET /api/profile` | **行为变更（可控）** | `?async=1` → `{taskId,status:"running"}`，结果经 `/api/task` 取；**不带 `async` 仍走同步旧路径**（受 max_msgs 保护）。前端本轮切 async，旧路径留一版兜底，下轮删。理由：CLI/MCP 间接依赖 `ProfileDistiller`，一次切换爆炸半径不值 |
| `/api/profile`（结果） | 新增 `summary.truncated:bool` | 命中 `max_msgs` 上限为 true，前端提示「样本已达上限，仅基于最近 N 条」 |
| `GET /api/personas` | **值变化** `current` | 首次启动 `"mimic"`→`"listener"`；字段名/类型不变，前端零改动 |
| `POST /api/chat` | **副作用变化** | 各人格上下文互相隔离（内部 `{cid}::{persona}`）。**响应字段完全不变** |

### 4.2 `/api/task` 结果形状（画像复用现成契约）

```
{ id, kind:"profile", status:"running|done|error", cost,
  result:{ ...同原 /api/profile 同步返回体，额外含 summary.truncated }, error }
```
`kind` 新增取值 `"profile"`（现有 `keywords`/`html-export`/`text-export`/`persona`/`moments-probe`），前端按 kind 分流处需加这一支。

### 4.3 前端需同步改动

| 文件 | 位置 | 改什么 |
|---|---|---|
| `views-insight.js` | Stats(L162) | `totalMessages===0` → 空态；磁贴脚注加 deduped 说明 |
| `views-insight.js` | Profile(L596+) | 改 taskId 轮询；`truncated` 提示 |
| `views-insight.js` | 时间分布图 | **无需改**（后端补零后自动全量） |
| `views-insight.js` | 群画像 | 加「按『我 vs 群』二分」免责说明 |
| `views-data.js` | `typeName`(L576) | 拆 `contactTypeName`/`sessionTypeName` 两张表 |
| `views-data.js` | 左栏(L415) | `!list.length` 空态（两套文案） |
| `views-setup.js` | LLM 卡片 `card-sub`(L23) | 末尾加「AI 对话默认以『倾听者』开口，随时可切」 |
| `views-console.js` | — | **无需改动**（人格高亮自动跟随 `d.current`） |

### 4.4 验收总表

| 项 | 断言 |
|---|---|
| D1 | `DEFAULT_PERSONA=="listener"`；`SWITCH_HELP` 含「默认倾听者」；两人格各 2 条历史互不可见 |
| D2 | 浅 `--brand`+白字≥4.5；浅渐变**四标逐一**≥4.5；深 `--brand`+`#0d1117`≥4.5；焦点环≥3；`logo.svg` 仍含 `#07c160` |
| D3 | 无 PNG 时 `.desktop` **不含** `Icon=` 且仍以 `[Desktop Entry]` 开头；各降级路径退出码均 0 |
| #6 | `_session_type("wxid_gh…")` 补 `wxid_gh` 前缀后与 `contact_type()` 一致返回 `mp`；`wxid_user01` 两处分别为 `other`/`private`（**此处不一致是设计使然，非缺陷**）；前端 `contactTypeName`/`sessionTypeName` 不再互相污染 |
| D4 | README 抓到的所有「N 工具」数字 == `len(TOOLS)` == 12；README 中 `mosu_*` 名称集合 == `TOOLS` 名称集合（双向） |
| #3 | `contact_profile(max_msgs=60000)` 命中上限时 `truncated is True` |
| #4 | `max_msgs=999999` 被拒 |
| #7 | `report(session=…)` 返回含 `summary.firstMessage/lastMessage` 且 `content` 非空 |
| #9 | `time_stats(by="hour")` 恒 24 桶、`"weekday"` 恒 7 桶、`"month"` 不补 |
| 追加 | 两处 `spanDays` 同为 `float` |
| 全量 | `C:\Python314\python.exe -m unittest discover -s tests -q` → `OK` 且 `Ran N tests` 中 N ≥ 154+新增 |