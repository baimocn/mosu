# 更新日志

本项目的重大变更都记录在此。格式遵循
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### 新增

- **洞察四模块技术文档**（`docs/modules/`）：逐模块写清架构、数据结构、接口契约、
  性能边界与已知问题，与代码逐条对齐
- **AI 的四种可切换角色**（`core/bot/personas.py`）：倾听者 / 分析者 / 安慰者 / 模仿者。
  每个角色有自己的身份、红线、**可看数据范围**与长度上限——倾听者与模仿者拿不到
  任何数据，因此默认状态下 AI 无法编造关于你的事实
- **事实层**（`core/bot/facts.py`）：把数据库统计压成可直接引用的短事实（往来体量、
  发起模式、回复间隔、情绪、话题），带口径标注与超时保护
- **对话上下文持久化**（`core/bot/store.py`）：常驻进程重启后能接上话，每会话保留
  最近 20 轮，磁盘故障静默降级为纯内存
- **手机版图片查看器**（`gui/assets/lightbox.js`）：缩略图打底 → 原图加载 → 失败重试
  → 滚轮缩放拖拽
- **朋友圈图片代理**（`core/remoteimg.py`）：带 token 抓微信 CDN 并落盘缓存，含
  24 小时可用性探测缓存
- **桌面快捷方式与应用图标**：双击桌面即用；常驻三通道自动降级（计划任务 →
  schtasks → 启动文件夹守护）
- **左侧导航改用手绘线性 SVG 图标**（`gui/assets/icons.js`），替换全部 emoji；
  界面动作（折叠/主题/命令面板/灯箱关闭）也统一到同一套图标

### 修复

- **洞察模块 8 处数据正确性缺陷**（多智能体并行审查发现）：
  - 高频词任务前后端字段名不一致（后端 `taskId` / 前端读 `d.id`）导致功能静默失效
  - 画像星期下标错位一天（SQLite `%w` 的 0 是周日）
  - `/api/contact` 的子串兜底可能命中**另一个人**并展示其数据
  - `social` 以 displayName 为键，两个同名会话被合并成一个人
  - `insights` 声称「周末输出高于工作日」，但代码从未比较过两组总量
  - `questionRate` 只统计半角 `?`，中文全角 `？` 全漏
  - 画像采样取到每个会话**最早** N 条而非最近 N 条（14 万条大群只贡献开头部分）
  - 空库时 `render_markdown` 对 `peakHour=None` 格式化崩溃
- **`truncated` 恒为 false：上限生效了却没人知道**（前端在真实数据上撞出）。
  14.9 万条消息的群画像只取回 59994 条、丢弃约 9 万条，`meta.truncated` 却报
  `false`，用户不会被告知。根因是判据写成了「多取 1 条看是否超过上限」，而
  `raw_messages` 的**去重发生在跨分片合并之后**——每个分片各取 limit 条，合并时
  重复行被丢弃，合并后总数必然 ≤ limit，所以 `len(rows) > limit` 恒为假。
  新增 `Dataset.raw_messages_page()` 显式回报是否截断，判据改为**去重之前**：
  任一分片能取到第 `limit+1` 条即视为截断（探针行不参与去重与返回）。
  `raw_messages()` 保持原签名，现有调用方零改动
- **本人画像（`self_profile`）同样会被采样截断却不上报**：23 万条的库必然触到
  60000 上限，但 `meta` 里没有 `truncated` / `maxMsgs`，本人画像页永远不会提示。
  现已与联系人画像用同一套契约补齐
- **同一公众号账号在两个页面显示不同类型**：`_session_type` 只判了 `gh_` 前缀，
  漏掉 `wxid_gh`（公众号的 wxid 形式），于是「数据浏览」页显示「单聊」、
  「联系人」页显示「公众号」，`session_type=2` 时还会显示成「群聊」。
  前端曾用同一张中文映射表把 `private` 翻成「好友」，恰好把这个矛盾盖住了。
  现在两处判定对齐（`core/dataset.py`）。**两套词表本身不合并**——通讯录看
  `delete_flag`、会话看 `session_type`，各有存在理由
- 聊天记录里的群聊消息残留 `wxid_xxx:` 发送者前缀
- 界面一致性：设置齿轮缺 2 齿且左右不对称；导航选中态蓝底配绿字；焦点环与图表
  色板残留蓝色；折叠态分组标题与徽标溢出、图标未居中；命令面板选中行绿标题配灰图标

### 变更

- **应用 Logo 重做**：定位从「数据溯源工具」调整为「倾听者 / 陪伴式对话助手」，
  图形改为「对话气泡 + 三道声波弧」，保留微信绿品牌底
- 品牌色由蓝改为微信绿（令牌、渐变、焦点环、图表色板统一）
- 统计接口的字段标签改为中文可读（占比显示 `50%`、时长显示「4 分钟」）；
  年报改为专属版式，不再倾倒原始 JSON
- 统计接口统一返回 `taskId`（此前混用 `id`/`taskId`，前端读不到）
- **默认 AI 人格改为「倾听者」**（原为「模仿者」）：logo 与文案都把墨溯定位成陪伴助手，
  默认人格却是模仿者，等于产品承诺与第一句话不一致。倾听者「不给建议、不看数据」，
  也是四条红线里最不容易出错的一条
- **对话上下文按人格分桶**（`{conv_id}::{persona}`）：分桶而非过滤——过滤会让
  「切人格后 AI 忘了刚才说的话」，分桶既保住同一人格内的连续性，又杜绝跨人格串味。
  存量不带 `::` 的历史文件自成旧桶，不迁移不删除
- **微信首次回复后告知可换风格**（进程内只发一次，不落盘）：微信里没有下拉框，
  对方只看到一个陌生账号在说话，不知道能发 `/模仿`、`/分析`、`/安慰`
- **统计口径透明化**：`global_stats` / `session_stats` 新增 `deduped: false`。
  统计走 `count(*)` 不去重而浏览页去重，两页数字会不等——写进返回值比让用户自己
  发现「统计怎么比列表多」要好。写死 false 是诚实的，将来真去重再改 true
- **时间分布补零**：`hour` 恒 24 桶、`weekday` 恒 7 桶。只返回出现过的桶会让柱状图
  **在视觉上撒谎**——凌晨没消息的小时不画，看起来像「数据缺失」而非「真的没有」。
  `month` / `date` 刻意不补（空月份是噪声）
- **统计接口支持时间范围**：`/api/stats/global|contacts|time` 各接受可选
  `from` / `to`，缺省＝全量，既有行为不变
- `/api/profile` 新增 `?async=1` 走 TaskHub（结果经 `/api/task` 取）。**不带该参数
  仍走同步旧路径**——CLI / MCP 间接依赖 `ProfileDistiller`，一次切换爆炸半径不值
- **README 的 MCP 工具数加了防腐化测试**：本轮出现过一次「README 写 12、实际 16」的
  误传，复核后发现两边都没错（是复核方数错了）。README 里的数字是对的，因此**一个字
  都没改**——写死数字让读者能自己核对，而「写死数字会腐化」这个风险交给
  `tests/test_docs.py` 锁住：README 里所有「N 工具 / N tools」必须等于 `len(TOOLS)`，
  反向再锁 `len(TOOLS) == 12`，另加工具名集合双向断言
- `report()` 返回体新增 `summary.firstMessage` / `summary.lastMessage`
  （会话口径；非会话为 `null`）——这两个值此前已查出来却没进返回值，纯浪费
- `contact_profile` 加上限 `CONTACT_MAX_MSGS=60000`，命中时 `overview.truncated=true`。
  原先 `raw_messages(limit=10**9)` + 同步持锁，是唯一能打挂整个服务的路径
- **⚠️ 行为变更：`contact_profile` 的采样方向从「最早 N 条」改为「最近 N 条」**
  （`order="asc"` → `"desc"`）。画像的用途是"了解这个人现在的样子"，取最早 cap 条
  等于让两年前的聊天决定今天的画像——与本轮修掉的「画像采样方向反了」是同一类错误。
  取数降序后内部**排回升序**再统计，`milestones.firstMessage` / `lastMessage` 语义不变。
  **副作用**：14.9 万条大群的话题与风格统计会改变（从开头 6 万条变为最近 6 万条），
  这是预期内的正确行为。截断时 `milestones.firstMessage` 会带 `sampledOnly: true`
  ——样本内最早一条并非会话真实首条，不标注会被前端当"他们第一次聊天"展示
- `keyword_stats` 服务端硬上限从 200000 收到 20000，`_texts()` 每 5000 条让出一次 GIL
- 快捷方式图标：macOS 生成 `.icns` 并放桌面（`.command` 在 Finder 侧绑不了图标），
  Linux 按 freedesktop 规范写 `Icon=` 指向已安装的 PNG。四级降级，任何一级缺失都
  不影响入口可用，退出码恒 0
- `remove_shortcut()` 扩展到 macOS / Linux（原先非 Windows 直接返回）。
  **不删** `~/.local/share/icons` 下那份——那是共享目录，别的 `.desktop` 可能正引用
- `spanDays` 本人画像与联系人画像统一为 `float`：JS 里 `3 !== 3.0` 为 `true`，
  类型不一致会让跨页分段判断错乱

### 视觉与交互升级（原生 CSS，零依赖）

- 主按钮 Aurora 渐变流 + Shiny 扫光 + Soft Glow 悬停光晕；加载态不再抖动布局
  - 玻璃拟态（Liquid Glass）令牌；输入框聚焦品牌光环；卡片悬停抬升
  - 控制台：思考点动画（Thinking Dots）+ 回复打字机流式 + 可点击建议问题
  - 统计页：**活跃度热力图**（GitHub 式一年日历，活跃天数/最长连续天数）、
    指标磁贴化（带趋势语义配色，涨红跌绿）
  - 密钥扫描进度从"滚动条"升级为**进度环**（阶段加权 + 阶段内细分进度）
  - 空态全部换为细线 SVG 插画（随主题变色），聊天消息悬停显示复制按钮
- `strip_sender_prefix`（`core/msgparse.py`）

### 修复

- **群聊消息正文残留 `wxid_xxx:` 发送者前缀**（CLI 导出与 GUI 同时受影响），
  现已在解析层剥除；URL 冒号结尾的行不会被误伤
- 群成员拿不到昵称时界面直接显示原始 wxid，改为优雅兜底

## [0.3.0] - 2026-10

### 新增

- **密钥自动获取**（`mosu key find` / GUI 首启向导）——新电脑零门槛：
  - 只读内存扫描（`ReadProcessMemory`，不注入不 hook），微信 4.1.15.13 实测
    还原密钥与 HMAC 权威校验逐字节一致，约 30 秒
  - 原理：版本 XOR 掩码从 `Weixin.dll` 代码段静态提取（零依赖 PE 解析），
    堆内结构锚点定位候选，PBKDF2+HMAC 并行校验（hashlib 释放 GIL，8 线程）
  - 环境预检 `/api/key/selftest`（进程/dll/掩码/数据目录逐项检查）
  - 失败降级：手动填密钥 / 诊断摘要（不含敏感值）交给任意大模型分析
- **GUI 全面重做为工业级界面**（设计系统 + 应用外壳 + 组件化）：
  - 设计令牌双主题（浅/深/跟随系统），改主题不动组件
  - 侧边栏分组导航、命令面板（Ctrl+K）、Toast、骨架屏、空态、灯箱
  - 首启向导：欢迎 → 环境检测 → 一键取密钥（SSE 实时进度，可取消）→ 完成
  - 数据浏览：会话搜索/筛选高亮、按天分组、连续消息归并、向上翻页保滚动位
  - 统计/画像/年报自适应渲染（结构化递归组件，任何数据结构不白屏）
  - 设置页：密钥管理、大模型配置（URL/API Key/模型 + 连通性测试）、外观
- **大模型配置持久化**（`core/llmconfig.py`）：GUI 里填 URL/API Key/模型名，
  落 `~/.mosu/config.json`；环境变量优先；`/api/llm/test` 真实连通性测试；
  LlmClient 自动读配置（核心功能依然全离线，不需要大模型）
- `mosu key find` 子命令（`--no-save` 只看不写）
- `gui/assets/`：前端拆分模块（theme/app.css + core/views/app.js），无构建链

### 修复

- `/api/status` 的 `data.ready` 误用懒加载状态（进程刚启动必然 false，
  导致每次重启都误弹首启向导）→ 改为判定配置链（密钥+目录）
- 撤回消息显示原始 `<sysmsg>` XML → 翻译成「撤回了一条消息」等可读文本
- `/api/messages` 契约补齐：稳定 id、createTime、senderName/senderId、
  hasMore 分页元数据（前端向上翻页与归并依赖）
- 静态资源路由泛化为 `/vendor/` + `/assets/` 双目录（前端拆分后所需）

## [0.2.0] - 2026-10

### 新增

- **GUI 本地控制台**（`mosu gui`）：浏览器打开 `127.0.0.1:8788`，五个标签页
  - 控制台：数字分身对话调试 + 微信消息流水 + 状态详情
  - 数据浏览：三栏（会话列表 / 微信气泡消息流 / 全库搜索），
    聊天图片解密直出（`/api/media?md5=`）
  - 数据统计：全局卡片 + 小时/星期/月分布 + 联系榜 + 关键词 Top30
    （自绘 SVG 图表，不引图表库）
  - 朋友圈：正文/图片/点赞/评论信息流，offset 分页
  - 导出：HTML zip / JSON / CSV / TXT，异步任务 + 耗时回显
  - 常驻双通道：Windows 计划任务优先，AtLogOn 被策略禁时自动降级
    启动文件夹 + VBS 守护
- **微信通道**（`mosu wechat`）：微信官方 Bot API（iLink）自研客户端，
  扫码登录 / 长轮询收发 / 游标持久化 / context_token 管理 / 心跳判活 /
  常驻安装（`login/serve/status/install/uninstall/task`）
- 数字分身 bot（`mosu bot`）：云端 LLM（OpenAI 兼容，任意后端）+
  RAG 风格锚点 + 规则硬校验（AI 腔/长度/感叹号/照抄检测），
  端到端消融验证三组件缺一不可
- `Moments.entries()` 支持 offset 分页与 `meta.cost` 耗时回传
- `Dataset.collect_session_rows()`：跨分片消息收集归位数据层
  （CLI 与 GUI 导出共用）

### 修复

- SQLite 跨线程崩溃：多线程 HTTP + 懒加载 Dataset 时连接被跨线程复用，
  `conn()` 加 `check_same_thread=False` + 数据 API 全程 RLock 串行
- VBS 守护脚本 `Set q = Chr(34)` 类型不匹配静默崩 → `q = Chr(34)`
- PowerShell 5.1 输出 GBK 导致 Python subprocess 读线程 UnicodeDecodeError
  → `encoding="utf-8", errors="replace"`
- `wechat get_updates` 曾把异常全吞（token 失效时假装在跑），
  现区分「超时=正常控制流」与「4xx/5xx=真故障」
- `bot` prompt 曾把风格约束与知识边界混在一条规则里
  （语料外话题一律答「不知道」），已解耦；长度校验上限 80→200

### 安全

- GUI 导出/静态资源防目录穿越（`resolve()` + `startswith`，
  `../` 与 `..%2f` 均 404）；导出产物目录固定，无任意文件读写
- 前端消息文本全走自动转义；SVG 图表用 `v-for` 渲染不拼 HTML 字符串
- `.env`、`persona/`（真实语料）、`.cache/`、`export/` gitignore

### 测试

- 离线测试 37 → 90 例（新增 GUI 导出序列化 / zip / TaskHub 覆盖）

## [0.1.0] - 2026-10

### 新增

- 自有 SQLCipher 4 解密引擎（PBKDF2-HMAC-SHA512 × 256000，
  AES-256-CBC + HMAC-SHA512），零 DLL、零网络、零授权检查
- 消息/联系人/群/语音/视频/朋友圈全格式解析，多分片自动合并去重
- CLI 13 组命令 + 五种输出格式（json/jsonl/table/csv/markdown）
- MCP Server（12 工具，stdio）
- 画像蒸馏（全库画像 + 单联系人深度画像）+ 离线中文情感分析
- HTML 气泡导出（图片视频内嵌）+ 增量导出 + 多账号切换
- 媒体解密：图片 V0（XOR）与 V2（私有容器 AES-128-ECB + 尾部 XOR）、
  语音 SILK → MP3
