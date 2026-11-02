# 墨溯 × Lancer 对标报告

> 对标对象：[Lancer](https://github.com/raylee-ai/lancer)（MIT，基于 pi-gui v1.0.1 + pi）
> 研读方式：通读 `packages/session-driver`、`packages/pi-sdk-driver`、`packages/light-ui`、
> `apps/desktop/electron/orchestration`、`apps/desktop/src/features/conversation`、
> `apps/desktop/contracts`、`docs/`（14 篇）、`.agents/skills/verify-pi-gui/`（10 篇）
> 本报告日期：2026-11-02

---

## 一、一句话结论

**Lancer 是「通用编排外壳」，墨溯是「垂类数据内核」。**
Lancer 完全没有真实数据能力（它的数据只有自己的会话记录）；
墨溯有真实数据内核（23 万条微信消息），但**没有编排外壳**——没有多代理、没有分支树、
没有回执、没有预算、没有派活通道。

对标不是「墨溯功能比 Lancer 少」，而是：
**把 Lancer 的编排外壳移植到墨溯的数据内核上**，而不是长成另一个 Lancer。

### 明确不抄的（有意取舍）

| Lancer 的东西 | 不抄 | 理由 |
|---|---|---|
| Electron 桌面壳 | ❌ | 墨溯是 Python + 浏览器控制台，零构建是铁律，Electron 是倒退 |
| Git worktree 集成 | ❌ | 墨溯没有代码仓库概念，分支隔离用「独立 Dataset 实例」更自然 |
| 9 个模型家族目录 | ✅ 抄（简化版） | 迭代 6 已实现 9 家族识别 + 分组 + 三态；但**不抄** SVG 品牌图标（用文字徽标 + 家族色，零外部资源、不引构建链），**不抄** App 直连入口（墨溯没有 GUI 应用直连通道） |
| 视频流水线（抽帧/复刻/剪辑） | ❌ | 垂类无关 |
| AgentMail / 订阅账号接入 | ❌ | 墨溯走 OpenAI 兼容端点，够用 |

---

## 二、能力矩阵

| 维度 | Lancer | 墨溯现状 | 差距 | 优先级 |
|---|---|---|---|---|
| **Agent 内核** | Pi SDK + extension 注入工具 | 自研 ReAct（`core/agent_engine.py`，MAX_STEPS=8） | 无缺口，各自可控 | — |
| **数据层** | 只有自己的会话 JSONL | SQLCipher4 自研解密 + 23 万条真实消息 | **墨溯更强** | — |
| **编排工具** | extension 注入 `create_child_thread` / `list_threads` / `read_thread` / `send_message_to_thread` | 无 | 缺 | **P0** |
| **分支树** | `OrchestrationChildThread` + 5 态状态机 + 父子双向索引 | 无 | 缺 | **P0** |
| **回执协议** | `reported → accepted`，**投递确认**（报告送达父会话才翻状态） | ✅ **迭代 11**：`consume_branch()` 程序化标记送达，孙分支的送达方是**直接父**（不跳级） | 无缺口 | — |
| **预算准入** | 4 维（并发/深度/节点/token），首个 await 前占位防超订 | 单维 MAX_STEPS=8 | 缺 | **P0** |
| **分支落盘追溯** | `.lancer/branch-reports/<sha>.json` + 重启重建 | 无（`AgentRun` 只在内存，任务淘汰即失） | 缺 | **P0** |
| **子树结算** | 三层递进（终态 + 子分支递归结算 + 无 pending 回执） | ✅ **迭代 11**：`branch_settled()` / `settle_children()` 真实接线到 `_settlement_of()`；未结算如实报警并点名分支 | 无缺口 | — |
| **显式接受（附加层）** | `orchestrator-accepted:` 文本标记 → 接受证据 | ✅ **迭代 11**：`mosu-accepted:` 程序化正则提取，落到 `acceptedBy`；**刻意不参与结算判定**（Lancer 全仓无提示词教模型写它） | 无缺口（定位为加分信息） | — |
| **跨泳道语义边** | `processTreeRoutes()`：reference / retry 两类虚线，跨泳道连接事件 | ✅ **迭代 12**：虚线独立图层 + 独立配色 + hover 说明理由；判据是墨溯自己的（都读过同一个人 / 同一话题），锚点读不到整条丢弃 | 无缺口（**抄纪律不抄判据**） | — |
| **证据账本** | 8 种 kind + severity + git 快照 | ✅ **迭代 10**：证据带 `source`（db/message/agent/model）+ `status`（verified/reported）+ 结构化 `scope`（口径四字段）+ 推断标注独立成 `inferences`；数字审计分主代理实测 / 子代理转述两池 | 无缺口（只抄「来源可核验」，不抄 severity / 编排生命周期） | — |
| **流式反馈** | 事件溯源 + callId upsert + 50ms 节流 | 900ms 轮询 `/api/task` | 差距明显 | P1 |
| **统一订阅通道** | 单条 `state-changed` 广播全量快照 | ✅ 派活通道 NDJSON 事件流（迭代 9：全局单调 seq + `since` 游标 + 凭证脱敏），GUI 侧洞察共用同一本日志 | 部分有（前端未接入实时渲染） | P1 |
| **外部派活通道** | Control API（Unix socket + token，`send`/`queue`/`steer`/`wait`/`tail`/`stream`/`stop`） | ✅ 本地 TCP + token，`send`/`queue`/`steer`/`status`/`runs`/`wait`/`tail`/`stream`/`stop`（迭代 9 补齐流式与取消） | 无缺口 | — |
| **多模型目录** | 9 家族 + 自定义 + 可用/未启用/未配置三态 | ✅ 9 家族 + 多端点 + 三态（迭代 6，`core/model_catalog.py`） | 无缺口 | — |
| **阶段流水线** | 1–8 阶段，各配 model/skills/goal/deliverables | ✅ 5 阶段契约（迭代 7）+ **阶段模型路由（迭代 8）**：取证 / 成文双角色，谓词 `distinct_configured_model` | 无缺口（只抄「角色分工」，不抄固定管道） | — |
| **分支可视化** | GitTree + 语义锚点光线 + 流动边框 | 5 视图平铺，无分支视图 | 缺 | P1 |
| **验证纪律** | 6 级证据分级 + 明确列「哪些没跑」+ flake 当 bug | 660 测试 + E2E + 存量失败未分级 | 部分有 | P1 |

---

## 三、P0：直接抄的五件事

### P0-1 编排工具：模型能自己派子任务
Lancer 把 `create_child_thread` 做成 extension 注入的工具，父 agent 自主调用；
**孙分支就是子分支自己调同一个工具**，递归天然成立，靠预算限深度。

墨溯版：新增 `delegate_tasks(tasks: string[])`，一次派 2–5 个可独立查证的子任务，
子代理跑自己的 ReAct 循环，回传「结论 + 证据 + 它查了什么」。
深度上限 2（主 → 子 → 孙），孙层不再看到该工具。

### P0-2 回执状态机：父级显式核对
Lancer：子分支完成只产生 `reported` 回执，父 agent 核对后才升级 `accepted`，
**只有 `accepted` 才在 GitTree 生成合并节点**。

墨溯版：子代理结论默认标「待核对」，父 agent 综合时必须自己核；
证据池保留每条证据的来源分支（`via: 分支N`），前端可追。

### P0-3 预算准入：树级四维限额
Lancer 的 `task-limits.ts`：

```ts
concurrent: limit("LANCER_MAX_CONCURRENT_BRANCHES", 8, 64),
depth:      limit("LANCER_MAX_BRANCH_DEPTH", 4, 16),
children:   limit("LANCER_MAX_TREE_BRANCHES", 32, 256),
tokens:     limit("LANCER_MAX_TREE_TOKENS", 500_000, 100_000_000),
```
`reserve()` 在**第一个 await 之前**占位，避免并发超订。

墨溯版：单轮 MAX_STEPS=8 变出来后，总步数会爆炸 → 必须有树级预算
（`max_branches` / `max_depth` / `max_total_steps` / `max_tokens`）。

### P0-4 并行加速的真正瓶颈在 LLM，不在 SQLite
墨溯的工具查询是毫秒级，LLM 推理是秒级。
**并行子代理 = 并行 LLM 调用 + 串行工具执行**，可拿到接近线性的加速。
前提：SQLite 连接跨线程必须串行化 → 锁下沉到 `core/locks.py`，全局共用一把 RLock。

### P0-5 分支树落盘追溯
Lancer 把编排记录落盘，重启后由保存记录重建——不是从日志猜。
墨溯版：`~/.mosu/runs/<run_id>.json` 存完整分支树 + 证据 + 步骤，
洞察页可回看历史，导出物可关联到具体分支。

---

## 四、P1：值得抄的四件事

### P1-1 统一订阅通道 + 增量 diff
Lancer 全靠单条 `state-changed` 广播全量快照，前端用 `${updatedAt}:${status}` 做
revision 脏标记，500ms 去抖后只重载受影响会话。
墨溯版：一条 SSE 通道推洞察事件流，替代 900ms 轮询。

### P1-2 外部派活通道（墨溯独有价值的放大器）
Lancer 的 Control API 让**外部 CLI 派活给正在运行的 GUI 会话**，
且明确拒绝旁路（不直接读写会话文件、不抢 lease）。
墨溯现在只能被外部读（MCP），不能被外部驱动。
墨溯版：`mosu dispatch` 走本地 socket + token，`queue`/`steer` 语义必须有——
墨溯分析耗时长，插队和排队是刚需。

### P1-3 分支可视化：先做「树」，后做「光线」
Lancer 的光线 UI 建立在语义锚点（`data-anchor-key`）之上，**查不到锚点就放弃发射，
绝不落到邻近节点**。这是它最值钱的一条工程纪律。

墨溯版：先做分支树（父 → 子 → 状态 → 耗时），后做光线。
**颜色按泳道分配一次全链路继承**（`--lc` CSS 变量），不搞随机色。

### P1-4 验证纪律：6 级证据分级 + 明确写「哪些没跑」
Lancer 的核心不等式，逐条抄：
- Core/单测通过 ≠ 真机可用
- 文件存在 ≠ 工具可运行（doctor 要**实际执行** `--version`）
- 源码包 ≠ 安装包
- **合成/夹具数据通过 ≠ 真实微信数据通过**（对墨溯最关键）
- 阶段 gate 成功 ≠ 产物真的存在
- flake 一律当 bug 修，禁止重试/跳过/放宽超时

---

## 五、P2：暂不做

- ~~**多模型目录**~~ → **迭代 6 已做**（9 家族 + 多端点 + 三态）。
  没抄的部分：SVG 品牌图标（用文字徽标）、App 直连入口（墨溯没有这条通道）。
- ~~**阶段流水线**~~ → **迭代 7 已做契约层**（`core/pipeline.py`）。
  读 Lancer 的 `packages/video-learn-process/PROCESS.json` 才发现：
  它的「阶段流水线」本质是**声明式流程契约**，不是视频功能。
  抄了四样：status code 带 `ok_to_proceed`（机器可判的推进条件）、
  每阶段一个 gate 产物、forbidden 清单、每阶段一行机械摘要。
  **没抄固定管道**——迭代 2/3 的教训（模型被绑住手 → 用户批「ai 功能好差劲」）。
  Lancer 自己的 `forbidden` 里写着 `prose_handoff_messages`、
  `generic_prompt_compression_as_handoff`，它反对的正是用散文串阶段。
  墨溯的做法是**顺序反了但标准一样严**：Lancer 先定流水线再跑，
  墨溯先跑完再按流水线验收（契约层，不是编排层）。
  没抄的部分：**每阶段绑定不同 model**（Lancer 的 `stage.model` +
  handoff 的 `distinct_configured_model`）——**迭代 8 已做**，
  但落点不同：不抄固定管道，只抄**角色分工**（取证 / 成文双角色），
  谓词名与判定口径照抄 `distinct_configured_model`。
- **AgentMail / 订阅账号**：走 OpenAI 兼容端点已够。

---

## 六、迭代路线

| 轮次 | 内容 | 验收 | 状态 |
|---|---|---|---|
| **1** | `core/agent_team.py`（分支树 + 预算 + 并行子代理 + 回执）；`core/locks.py` 全局数据锁；`delegate_tasks` 工具接入 `agent_engine`；洞察页展示分支卡片 | 真模型 + 真数据跑通「分别分析 3 个人」；数字审计覆盖子代理数字；回归全绿 | ✅ 完成（commit `d6de7c6`） |
| **2** | 洞察事件流 SSE 通道，替代 900ms 轮询；分支树落盘 `~/.mosu/runs/`；洞察历史可回看 | 前端实时看到子代理进度；重启后分支树可重建 | ✅ 完成（commit `a461c14`） |
| **3** | 外部派活通道（本地 socket + token，`queue`/`steer`）；`mosu dispatch` CLI | 外部 CLI 能派活给运行中的墨溯 GUI，并等结果 | ✅ 完成 |
| **4** | 分支可视化（树 → 语义锚点光线）；多模型目录 | 颜色按泳道继承；锚点缺失不乱飞 | ✅ 完成（commit `af8301a`，可视化部分） |
| **5** | 验证文档升级为 6 级证据分级 | 每份报告明确列「哪些没跑」 | ✅ 完成（commit `78dd7cf`） |
| **6** | 多模型目录（9 家族识别 + 多端点 + 三态 + 模型选择器 UI） | 模型名优先于 provider 归类；切换模型下游真的生效；旧配置继续可用 | ✅ 完成 |
| **7** | 洞察阶段契约（`core/pipeline.py`）：5 阶段 + status code 带 ok_to_proceed + 每阶段 gate 产物 + forbidden 清单 + 机械摘要 | 契约随答案返回；判定用原始答案不自证；前端阶段带三态可区分 | ✅ 完成 |
| **8** | 阶段模型路由（`core/llmconfig.py` + `core/agent_engine.py`）：取证 / 成文双角色 + `distinct_configured_model` 谓词 | 配了不同模型 → 成文真跑且谓词成立；配同名 → 明确点破不假装；不配置 → 行为一字不变 | ✅ 完成 |
| **9** | 派活通道补齐 Lancer Control API 的三样：`stream`（NDJSON 事件流 + `since` 游标 + 凭证脱敏）/ `tail` / `stop`（协作式取消） | 订阅端实时看到工具轨迹；`stop` 真打断且 `wait` 看到 `cancelled`；空闲时明确报 `NOT_CANCELLABLE` | ✅ 完成 |
| **10** | 证据账本升级（对标 `OrchestrationEvidenceRecord`）：来源区分 / 口径 / 数字分池 / 推断标注 | 每条证据带 source+status+scope；口径随查询条件变；推断句与事实句分开摆；非文本消息不产空白证据 | ✅ 完成 |
| **11** | 子树结算 + 回执协议（对标 `settled()` / `branch-return` / `orchestrator-accepted`）：投递确认式回执 / 三层递进结算 / 显式接受独立层 | 送达点真在 payload 回喂之后；孙分支送达方是直接父；未结算如实报警；结算落盘可回看 | ✅ 完成 |

### 迭代 1 实测记录（补充）

- 峰值并发 **3 路**，串行耗时和 31.0s → 实际墙钟 10.5s，**加速比 2.95x**
- 端到端抓出 5 个单测抓不到的真 bug（session 漏传 ×2、提示词漏工具、MAX_STEPS 不足、wxid 泄漏）
- 回归 733 passed / 16 skipped / 0 failed

### 迭代 2 实测记录（补充）

- SSE 首事件延迟 **0.00s**，step 增量陆续到达（2.38s → 6.99s），done 在 11.02s
- 落盘答案与 SSE 结果**逐字一致**
- Playwright 真机点提问：**2.0s 就看到实时轨迹**，无 JS 错误
- 测试抓出 3 个真 bug：run_id 撞名静默覆盖、摘要/详情同名不同型、**丢失唤醒**（每个事件延迟满 1s）
- 反向验证：注入丢失唤醒 → 测试变红；移除 EventSource → 静态接线测试变红

### 迭代 3 实测记录（补充）

**研读来源**：Lancer 的 Control API（`apps/desktop/electron/control/`）。抄的是**语义**，
不是实现——Lancer 用 Unix domain socket 且明确拒绝 Windows（`control-server.ts:105`），
墨溯主战场是 Windows 且有零外部依赖铁律，所以改用 **localhost TCP + token**（只绑 127.0.0.1）。

抄过来的 5 条语义：
1. **统一信封**：`{ok:true, result}` / `{ok:false, error:{code, message}}`——所有错误都带机器可读错误码
2. **结构化错误码**：`BUSY` / `UNAUTHORIZED` / `NOT_FOUND` / `INVALID_REQUEST` / `TOO_LARGE` 等 8 个
3. **Bearer token + 常量时间比较**（`hmac.compare_digest`，普通 `==` 可被时序攻击逐字节猜出）
4. **1 MiB body 限流**（对应 Lancer 的 `MAX_BODY`）
5. **三种投递模式**：`send`（忙 → 明确拒绝）/ `queue`（忙 → 挂起排队）/ `steer`（忙 → 递进正在跑的那轮）

**实测**（真模型 + 真数据 + 真 socket）：
- `mosu dispatch send --question "我上个月跟谁聊得最多？" --wait` → **841 字真答案**，含 10 条证据表格，24 秒
- 忙时 `send` → `BUSY` + 提示「改用 queue 排队，或 steer 引导」；忙时 `queue` → 排队位置 1，当前轮跑完自动接上
- 忙时 `steer` → 引导真的进了 Agent 上下文（落盘 step 里能查到 `tool=steer`），**不是只存起来**
- 排队任务真跑完：24 秒，`wait` 拿到 2634 字结果
- 端到端 **16/16 通过**

**测试抓出 1 个真 bug（而且是端到端才暴露的）**：
CLI 用 `return 1` 表达失败，但 `main()` 把 `args.func()` 的**返回值丢了** → 进程照样 `exit 0`。
症状是「输出里明明写着 `[x] BUSY`，退出码却是 0」——脚本/CI 判断不了成败。
单测全绿时完全看不出来，是端到端断言退出码才暴露的。
修复：`main()` 统一把非 0 整数返回值转成退出码（并排除 `bool`，`True` 会被误判成失败）。
同类问题还影响 `cmd_key`（自动取密钥失败也是 `return 1`），一并修好。

**另一个补的缺口**：steer 步骤落盘时摘要原本是固定文案「已把外部补充指令并入当前分析」，
回看历史只知道「有过引导」，不知道引导了啥。改成记录引导原文（截断 80 字）。

**反向验证**：回退 `main()` 退出码修复 → 4 例退出码测试如期变红；恢复后全绿。

### 迭代 4 实测记录（分支可视化）

**研读来源**：Lancer 的 `apps/desktop/src/features/conversation/lancer-process/`
（`process-tree.tsx` 593 行、`process-curves.tsx` 177 行、`process-curve-routes.ts` 201 行）。

抄过来的 3 条工程纪律：
1. **树 = grid 列（泳道）× 行（事件）**，竖直 rail 用 `lane.color`
2. **颜色按泳道分配一次、全链路继承**——`ProcessLane.color` + `laneStyle(color)`
   下发 `--lc` / `--cc` CSS 变量。随机色每次刷新都变，用户没法把颜色和某个子代理对上
3. **锚点缺失不乱飞**（最值钱的一条）——`readCurveAnchor(root, key, origin): Point | null`，
   上层 `if (!a || !b) return;`，**放弃发射这条线，绝不落到邻近节点**。
   原注释原话：`a clipped anchor cannot be replaced by a nearby node`

墨溯版差异：Lancer 每帧重算（实时飞行动画），墨溯是静态布局；
Lancer 用 React + SVG 库，墨溯零构建原生 JS，所以曲线用原生 SVG path 手写三次贝塞尔。

**实现**：
- 后端 `core/agent_team.py`：`LANE_COLORS` 固定 8 色调色板（无重复色）、
  `Branch.lane`（**parent 优先于显式 lane** → 子分支继承父的泳道，整条链一个颜色）、
  `lane_color` property、`to_dict()` 下发 `lane`/`laneColor`
- 前端 `gui/assets/v2/views-branch.js`：泳道分列 + SVG 曲线层（`pointer-events:none`）
  + `data-anchor-key` **语义锚点**（不是坐标配对 → 列表变了线依然连对）
- **虚拟根节点**（本轮补的设计缺口）：只有一层分支时父子边一条都没有，
  「树」退化成几个并列卡片，拓扑完全看不出来。加一个代表主代理的虚拟根，
  每个顶层分支都有一条从根出发的线 → 一眼看出「主代理并行派了 N 个子代理」

**实测**（真模型 + 真数据 + Playwright 真机）：
- 3 泳道 / 3 节点 / 3 条根连线，颜色 `['#3b82f6','#a855f7','#06b6d4']` 各不相同，
  且继承到节点边框 `rgb(59,130,246)` 等
- 端到端 **15/15 通过**，console 0 错误

**测试抓出 2 个真 bug（都是端到端/真机才暴露的）**：

1. **Vue 局部组件注册方向搞反**
   —— 只在 `shell.js` 的 `App.components` 注册了 `branch-tree`，
   但 Vue 的局部组件注册是**向下**可见的：App 注册了，App 的子组件（Insight）里**用不了**。
   `<branch-tree>` 成未知标签，**Vue 静默忽略不报错**，页面看着正常但分支树永远不出现。
   修法：在用到的视图自己注册 + 脚本加载顺序必须在前。
   附带发现：**脚本加载顺序**也是坑——视图在定义时就要拿到 `global.MosuV2.BranchTree`，
   顺序反了 → 注册 undefined 组件 → 静默不渲染。

2. **重算只在 mount/resize 触发，DOM 布局变了曲线不跟上**
   —— 手动藏掉一个节点，连线**一条都没消失**（曲线还指着已经看不见的节点）。
   根因：布局会因为很多没注册事件的原因变（文本换行、面板折叠、字体加载、用户手动隐藏）。
   修法：rAF 持续重算 + **脏检查**（坐标没变就不动 Vue 状态，
   否则每帧 set → 重渲染 → 又改变布局 → 死循环）；比较用**半像素容差**，
   因为 `getBoundingClientRect` 会返回亚像素浮点，字面相等会来回抖动。

**一个值得记的测试陷阱**：端到端里「隐藏节点后连线全部消失」这条断言，
在连线数为 0 时会**空跑通过**（`before=0`，`after=0` 也满足 `after == 0`），
把 bug 完全盖住了。是加虚拟根之后连线数变成 3，这条断言才真正生效并变红。
→ **断言必须先确认前置条件成立**，否则等于没测。

**反向验证**（两组纪律各做一次）：
- 把「锚点缺失 → 跳过」改成「落到邻近节点」→ 2 例变红
- 把 rAF 持续重算改成空函数 → 3 例变红
- 恢复后 33 例全绿

**新增的离线前端渲染测试** `tests/test_branch_tree_render.py`（11 例）：
用**固定数据**挂载真实组件（真浏览器），不跑模型、不依赖 GUI 服务，14 秒跑完。
这样以后改前端能秒级回归，而不是每次都等一次真机提问。

---

## 迭代 5 实测记录（验证纪律：6 级证据分级）

**研读来源**：Lancer 的 `.agents/skills/verify-pi-gui/SKILL.md`（92 行）、
`features/README.md`（验证特性地图）、`docs/ci-baseline.md`、
`docs/release-readiness.md`、`docs/platform-validation.md`、根 `AGENTS.md`。

Lancer 的核心资产不是代码，是**一整套关于「什么才算验证过了」的纪律**。
抄过来的 4 条：

1. **桌面活儿没在真 Electron 面上验过，就不算做完**
   （原话：Desktop work is not done until it is verified on the real Electron
   surface, not only by unit tests）
2. **一次设置页 smoke 不等于对话能用**
   （原话：A settings smoke does not establish a working conversation）
3. **平台验证要写明「这一轮没验什么」**——
   `platform-validation.md` 的 Windows 一栏原文：
   「Build/test configuration is present. ｜ **No native Windows acceptance in this round.**」
4. **每个 feature 的覆盖状态由实际跑过的 checkpoint 决定，不由 map 决定**
   （原话：A map is coverage intent, not a list of passed tests）

墨溯版落地：

- `docs/verification.md` —— 6 级证据分级（L1 静态 → L6 真机全链路）+
  每级「能证明什么 / 不能证明什么」+ 硬规则
- `core/verify_report.py` —— **机器可检查**的报告校验器（纯标准库）
- `docs/verification/*.json` —— 前四轮的结构化验证报告（4 份）
- `tests/e2e/lancer/` —— 4 个迭代验证脚本**入库** + 索引 README

**核心规则（用代码锁死，不靠自觉）**：
1. `not_run` 字段**必须存在**——字段缺失 = 报告不合格（沉默 = 假装全验了）
2. `level` **不得高于** claims 能支撑的强度（防虚报证据等级）
3. `not_run` 不能是空话（「其他场景未验证」这种范围无法界定的必须拒）
4. `evidence` 里的入库路径必须真的存在

**用新纪律反过来审计前四轮，抓出一个真缺口**：
迭代 1 交付的「多代理并行」当时只在**真实数据 + 脚本**层面验过（L4），
从没在真机上独立验过这个功能本身（L5）。
迭代 4 的端到端覆盖的是「分支树这个视图」，不是「多代理并行这个能力」——
两码事。按「L4 不能替代 L5」，这是实打实的缺口。

**补测结果**（真机、真模型、真数据）：
- 3 条泳道 / 3 个子代理 / 3 份互相独立的结论（各查各的对象）
- 洞察页明示「它派了 3 个子代理去查」
- **并行收益实测**：子代理耗时合计 162s，实际墙钟 36.1s（**4.5x**）
- console 0 错误 → **10/10 通过**，迭代 1 报告升到 L5

**测试抓出 1 个真 bug（关于测试本身）**：
反向验证时发现「去掉 `not_run` 必填检查 → 测试仍全绿」。
根因：`not_run` 的**类型检查**（不是数组就拒）把「缺字段」也拦下了，
两条检查互相掩盖 → **必填检查坏掉时测不出来**。
修法：① 缺字段与类型错报**不同的原因**（`缺必填字段` vs `必须是数组`）；
② 测试断言的是**具体那条原因**，不只断言「被拒了」。

这个坑和迭代 4 那个「连线数为 0 时断言空跑通过」是同一类：
**断言必须在正确的层面失败，否则它一直在假装工作。**

**反向验证**（4 条规则逐条破坏）：
- 去掉 `not_run` 必填检查 → **2 例变红**
- 去掉虚报等级检查 → **2 例变红**
- 去掉空话检查 → **2 例变红**
- 去掉 evidence 存在性检查 → **1 例变红**

**验证**：
- 校验器测试 **38 例全绿**（含 4 条反向验证）
- 仓库里 4 份报告全部通过校验器
  （`test_all_committed_reports_pass` 把这条接进回归）
- 全量回归 831 → **885 passed / 16 skipped / 0 failed**

**副作用**：把验证脚本从 `.cache/audit/`（gitignored）搬进 `tests/e2e/lancer/`，
换人接手能复现验证；报告的 `evidence` 指向入库路径，读者可核对。

---

## 迭代 6 实测记录（多模型目录：9 家族 + 多端点 + 三态）

### 抄了什么 / 没抄什么

Lancer 的 `apps/desktop/src/ui/model-families.ts` 只有 32 行，但它承载了一条关键规则。
原注释：

> Transport providers (including OpenAI-compatible gateways) do not determine
> model identity when the actual model or its catalog label identifies a family.

**这条规则是整个迭代的核心。** 用户拿一个 OpenAI 兼容网关转发 Claude，
provider 叫 `openai`，模型是 `claude-sonnet-4`——家族必须判成 claude。
反过来（provider 优先），所有经兼容网关的模型都会显示成「GPT」，
**而且不会报错**，只是看起来全错了。

墨溯的实现把 Lancer 的两个平行数组（`MODEL_FAMILIES` / `MODEL_NAMES` 靠下标对齐）
改成了**每个家族自带两套正则的 dict**——Lancer 那种写法改一个忘改另一个就会静默错位。

**没抄的**：
- Lancer 给 9 个家族配了 SVG 品牌图标（`ui/model-brand-marks.ts`）。
  墨溯零外部依赖 + 前端无构建步骤，塞一批 SVG 会变成「资源文件 + 加载顺序」的新坑。
  改用**文字徽标 + 家族色**，信息量不减、实现成本为零。
- Lancer 的「豆包 App 直连」入口——墨溯没有 GUI 应用直连通道，这条不成立。
- Lancer 的 `enabledModelPatterns` 精确到 `providerId/modelId`；
  墨溯放宽到也支持纯模型 id 与 `*` 通配（跨端点选模型是常见诉求）。

### 端到端抓到 4 个真 bug（都是单测抓不到的）

| bug | 现象 | 为什么单测抓不到 |
|---|---|---|
| `api_models_endpoint_save` 少 import `load_model_catalog` | 建端点/激活接口 **HTTP 500** | 单元测试走纯函数路径，不经过 HTTP 封装层 |
| `settings.model` 没登记进 shell.js 的 `SUB` | 深链 `/#/settings.model` 被静默忽略 → tab 停在「账号与密钥」→ **模型面板整块不渲染** | Vue 静默忽略 + 路由校验不报错 |
| `core/bot/llm.py` 的 `_load_dotenv()` 用 `setdefault` | 端到端永远读到本机 `.env` 里的真实网关，**测不到「切换模型是否生效」** | 只在有 `.env` 的机器上复现 |
| `views-models.js` 里两个同名 `const currentFamily` | `SyntaxError` → **整个组件挂掉** | 语法错误，Playwright 一加载就白屏（渲染测试抓到了） |

第 3 条促成了一个**生产代码的正向增强**：`MOSU_NO_DOTENV=1` 可禁用 dotenv 加载。
这不是给测试开后门——`.env` 优先级高于 GUI 设置是既有铁律，
但它让「在隔离环境验证配置切换」这件事变得不可能。加开关比改铁律划算。

### 反向验证（6 条规则逐条破坏）

- 模型名优先 → provider 优先 → **4 例变红**
- 三态「未配置」全改成「可用」 → **2 例变红**
- 模型项去掉 `disabled` 约束 → **3 例变红**
- 设置页去掉局部组件注册 → **1 例变红**
- 切换模型不回写扁平字段 → **2 例变红**
- 激活时不记录 `activeModel` → **原本 71 passed 全绿！**

第 6 条是**这轮最有价值的发现**：`activeModel` 驱动目录里的 active 判定，
而扁平 `model` 字段走的是另一条路。只测后者时，前者被破坏测试照样全绿。
补测后（`test_activate_records_active_model_in_catalog`）→ **2 例变红**。

**教训**：两条独立的数据路径（扁平字段 vs 目录 active），
必须各有一条断言——否则其中一条断了你不会知道。

**验证**：
- 新增单元测试 **36 例** + 静态接线 **23 例** + 离线前端渲染 **15 例**（真 Playwright，14 秒跑完）
- 端到端 **28/28 通过**（真起 GUI 服务、真发 HTTP、真点浏览器）
- 全量回归 885 → **961 passed / 16 skipped / 0 failed**

---

## 六之二、迭代 7 详细记录：阶段契约

### 读 Lancer 读出了什么

`packages/video-learn-process/PROCESS.json`（484 行）才是「阶段流水线」的真身。
它不是视频功能的实现细节，是一份**声明式流程契约**：

| 机制 | Lancer 的做法 | 墨溯抄不抄 |
|---|---|---|
| `steps[]` | 每阶段带 actor / inputs / outputs / writes_gate / success_status / fail_statuses / rules | **抄结构，不抄固定管道** |
| `status_codes` | 带 `ok_to_proceed`——状态不只是标签，是**机器可判的推进条件** | ✅ 抄（精华） |
| `forbidden[]` | `prose_handoff_messages`、`generic_prompt_compression_as_handoff`… | ✅ 抄 |
| gate 产物 | `_gates/<step>.<id>.json`，绑 sha256 防事后替换 | ✅ 抄结构 |
| 每阶段机械摘要 | *"One short line of ids, paths, hashes, and status codes. No narrative."* | ✅ 抄 |
| 每阶段不同 model | `stage.model` + handoff 要求 `distinct_configured_model` | ⏸ 迭代 8 |

### 为什么墨溯不抄固定管道

迭代 2/3 的教训：墨溯原来是固定管道（预塞 7 条事实 + 8 条语料 → 模型一次调用），
用户批「ai 功能好差劲」——**根因不是模型，是模型被绑住了手**。

而 Lancer 自己的 `forbidden` 里恰好写着 `prose_handoff_messages`、
`generic_prompt_compression_as_handoff`——**它反对的正是用散文把阶段串起来**。

所以墨溯的做法是**顺序反了，但标准一样严**：

- Lancer：先定流水线 → 再跑
- 墨溯：先跑完（ReAct 自主循环不变）→ **再按流水线验收**

契约是**约束层**，不是**编排层**。模型照旧自主选工具、决定查几步；
我们只对**已发生的事实**做机械判定。

### 墨溯的四阶段与判定

| 阶段 | actor | 关键？ | 判定依据 |
|---|---|---|---|
| `scope` 范围认定 | local | 否 | 有没有做过范围认定（resolve_time/find_contact/…） |
| `probe` 数据探测 | local | **是** | 有没有取到统计类证据（kind=stat） |
| `read` 原文取证 | local | 否 | 有没有取到原话证据（kind=quote） |
| `verify` 答案核验 | local | **是** | 答案里的数字有没有出处 |

状态码：`OK` / `SKIPPED`（不阻断）/ `EMPTY` / `MISSING` / `NO_QUOTE` / `UNSOURCED_NUMBER`。
每个都带 `ok_to_proceed`——**Lancer 的核心机制**。

forbidden 四条（迭代 7 时是四条，**迭代 8 加到五条**）：
`no_number_without_source` / `no_stat_as_quote` /
`no_answer_without_evidence` / `no_quote_without_read` /
`no_self_review`（成文模型与取证模型同名）。

### 踩到的坑

1. **判定必须用原始答案，不能用拼完审计提示的答案**
   ——审计提示本身含数字（「2904」会被写进提醒里）。
   先拼提示再判定 → 契约拿提示里的数字当答案的数字来自证 → **永远判成「有出处」**。
   修法：把 `_unsourced_numbers()` 从 `_audit_numbers()` 里拆出来，判定走原始答案。
   反向验证实测：把顺序换回去 → **1 例变红**。

2. **渲染测试抓不到「后端没把 contract 传给前端」**
   ——渲染测试用 mock 数据（contract 直接塞进 mock 返回），
   前端忘了 `turn.contract = res.contract` 它照样全绿。
   同理「后端忘了传」也抓不到。
   这两条只有**真链路端到端**能暴露。补断言后，破坏实现 → 端到端立刻红。

3. **`views-insight.js` 顶部就引用 `global.MosuV2.BranchTree`**
   ——离线渲染探针页只加载洞察视图 → 注册子组件时抛错 → **整个视图挂不上**
   （表现为 `__INSIGHT_READY__` 一直不置位，30 秒超时）。
   探针页必须先加载 `views-branch.js`。

4. **file:// 下 EventSource 必被 CORS 拦**
   ——报的是 `Access to resource at 'file:///…' … blocked by CORS`。
   这是**探针页的环境限制，不是组件 bug**（组件本就设计成 SSE 挂了自动降级轮询）。
   处理：探针页把 EventSource 换成桩，只触发 error 事件，强制走轮询保底路径。

### 实测记录

- 新增单元测试 **38 例** + 静态接线 **27 例** + 离线前端渲染 **12 例**（真 Playwright）
- 端到端 **27/27 通过**（真起 GUI 服务、真发 HTTP、真模型 + 真工具 + 真数据库、真点浏览器）
- 全量回归 961 → **1038 passed / 16 skipped / 0 failed**
- 反向验证 **8 条纪律逐条破坏，全部变红**（含 1 条只有端到端能抓的）
- 验证报告 L5（`docs/verification/iter7-insight-contract.json`），9 条「哪些没跑」

真机跑出来的真实判定（同一问题两次运行，模型行为不同）：
`scope:OK | probe:MISSING | read:SKIPPED | verify:UNSOURCED_NUMBER`
——契约**真的在拦**，不是摆设。

---

## 六之三、迭代 8 详细记录：阶段模型路由（取证 / 成文双角色）

### 读 Lancer 读出了什么

Lancer 的阶段模型机制在四个地方：

1. `apps/desktop/contracts/lancer-workflows.ts` —— 步骤上**可选**的模型绑定
   ```ts
   export interface LancerWorkflowStep {
     readonly title: string;
     readonly model?: { readonly provider: string; readonly modelId: string };
     readonly skills?: readonly string[];
     readonly goal?: string;
     readonly deliverables?: string;
   }
   ```
   阶段模型不是流水线内置的，是**步骤自己挂的**。校验规则：steps 1–8 个、`title` ≤40 字、
   `model.provider/modelId` ≤512 字。

2. `apps/desktop/electron/application/app-store.ts:1289` —— 切模型靠
   `setSessionModel(stage.model.provider, stage.model.modelId)`。

3. `packages/video-learn-process/PROCESS.json:234` —— **handoff 边的谓词**（本轮最关键的一行）：
   ```json
   {"from": "analysis.ok", "to": "handoff",
    "predicate": "verified_spec_sha256 && verified_frame_references && distinct_configured_model"}
   ```
   **交付阶段必须用与上游不同的模型**——防单一模型偏见。

4. `apps/desktop/src/features/conversation/video-learn-stages.ts:46` —— UI 显示
   `stage.model ?? stage.actor`，并用 `modelFamily("", stage.model)?.id` 显示模型家族。

另外一条容易忽略的细节：**handoff 读的是上游产物**（spec + manifest），
不是上游模型的散文自述。这正是交叉核验能成立的前提。

### 为什么墨溯不抄固定管道

Lancer 的阶段模型是「1–8 个流水线阶段，每阶段一个模型」。墨溯的 ReAct 是**自主循环**——
迭代 3 的教训已经很明确：固定管道会把模型绑死（「某人上个月聊了什么」它看不到记录）。

所以墨溯只抄**角色分工**，不抄**阶段切分**：

| 角色 | 职责 | 默认 |
|---|---|---|
| `collect` | 跑 ReAct 循环、查工具、收证据 | 活跃模型 |
| `write` | 基于**证据包**（不含取证者自述）写最终答案 | 未配置 = 空串 |

对应 Lancer 的 `stage.model` + `distinct_configured_model` 谓词，
但落点是「取证 vs 下结论」而不是「阶段 1 vs 阶段 2」。

### 墨溯要治的盲区

**取证和下结论是同一个模型**——它自己查的数据自己确认，自己没法质疑自己。
这正是 Lancer 那条谓词要防的事，只是 Lancer 防在阶段之间，墨溯要防在同一次问答内部。

### 四条设计判断

1. **distinct 判据是模型名不同，不是端点不同。**
   同一模型挂两个端点（网关转发）还是同一个脑子，交叉核验不了——
   Lancer 的谓词也是比 model，不是比 provider。

2. **`writeModel` 未配置时是空串，不回落取证模型。**
   回落了就分不清「用户选单模型模式」（不该报违规）与「用户配错了」（必须报违规）。

3. **成文模型看不到取证模型的自述。**
   只给工具返回的证据包。给了自述它就只会照抄，两个模型等于一个。

4. **不配置 = 现在的行为，一字不差。**
   向后兼容是硬要求。

### 踩到的坑（都是端到端抓到的，单测抓不到）

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | `applied=False`，composeError=「成文模型调用失败」 | `_resolve_models` 返回值**缺 `collect`/`write` spec 子键**——`_write_client()` 读的是 `spec.get("write")`，永远拿到 `{}` → 造不出客户端 | 返回里补完整 spec 子键 |
| 2 | 未配置成文模型却报 `no_self_review` 违规 | `write_model = write.get("model") or base_model`——未配置时回落到取证模型，`wm == cm` 成立 | 改成 `or ""`（空串 = 未配置） |
| 3 | 设置页下拉不显示已保存的模型 | `stage_models_view()` 的 `available` 只从端点模型表取；扁平配置下端点表是空的 | `available` 必须包含该角色**当前已保存的模型** |
| 4 | 落盘断言拿到的是错误那条记录 | 端到端用 `files[-1]`（文件名序）取最新——文件名序 ≠ 时间序 | 按 `createdAt` 排；且要找 `writeModel == WRITE_MODEL` 的那条（场景 C 后最新的是同名那条） |
| 5 | 成文永远失败 | `chat_tools(msgs, [], ...)` 传**空 tools 数组**，部分 OpenAI 兼容网关报 400 | 改用普通 `chat()`——成文阶段本来就不需要工具 |
| 6 | 成文客户端造不出会掀翻整轮 | `LlmClient` 无 key 时 `raise SystemExit`（BaseException），`except Exception` 抓不到 | `_client_for` catch `BaseException` |

**教训重申**：坑 1/2/5 只有**真跑两个不同的模型**才会暴露——
mock 造出 tool_numbers 就能让判定通过，但那不叫交叉核验。

### 实测记录

- 新增单元测试 **37 例** + 静态接线 **33 例**（`test_stage_models.py` / `test_stage_models_wiring.py`）
- 端到端 **49/49 通过**（真起 GUI、真发 HTTP、真模型 ×2、真点浏览器）
  - 取证模型 `deepseek-v4.1-flash`、成文模型 `glm-5.3`（**两个不同家族**）
- 全量回归 1038 → **1092 passed / 16 skipped / 0 failed**
- 验证报告 L5（`docs/verification/iter8-stage-models.json`），10 条「哪些没跑」

真机跑出来的真实判定（同一问题三次运行，三种配置）：

| 配置 | distinct | applied | compose | no_self_review |
|---|---|---|---|---|
| 不配置 | False | False | SKIPPED | 不报（单模型模式不算违规） |
| 配不同模型 | **True** | **True** | **OK** | 不报 |
| 配成同名 | False | False | SKIPPED | **报**（带 detail：「都是 deepseek-v4.1-flash，不构成交叉核验」） |

成文答案正文里真的带上了「（依据：…）」引用标记——
这是「第二个模型确实参与了」的硬证据，不是字段名对得上而已。

---

## 六之四、迭代 9 详细记录：派活通道的 stream / tail / stop

### 读 Lancer 读出了什么

`apps/desktop/electron/control/` 四个文件，1468 行。关键三处：

**① 服务端流式响应**（`control-server.ts`）：

```ts
if (!(result instanceof ControlStream)) { respond(200, {ok:true,result}); return; }
response.writeHead(200, { "Content-Type": "application/x-ndjson", "Cache-Control": "no-store" });
response.flushHeaders();
for await (const event of result) { response.write(`${JSON.stringify(event)}\n`); }
```

**② 订阅先于回放**（`control-stream.ts` 的注释，一字不差）：

> A subscription is installed before replay so a cursor never races live publication.

**③ 游标语义**（`control-service.ts:482`）：`since` 必须是非负安全整数，
`journal.logs(ref, since)` 回放 **`seq > since`** 的事件。

Lancer 的 `ControlStream` 是**常驻**的——`control-stream.ts` 里只有 `close()`，
没有任何「空闲就结束」的判定。

### 墨溯抄了什么

| Lancer | 墨溯 | 差异原因 |
|---|---|---|
| `stream` → NDJSON 逐行推 | ✅ `core/dispatch_journal.py` + `core/dispatch_server.py` | — |
| 全局单调 `seq` | ✅ 全局（不是每会话独立） | seq 只是游标，单调就够 |
| 订阅先于回放 | ✅ 同口径 | — |
| `since` 游标 | ✅ `require_cursor` 严格校验（拒 bool / 负数 / 小数） | — |
| `tail` → 末 N 条 + lastAssistant | ✅ 同口径（n 上限 1000） | — |
| `stop` → 取消当前轮 | ✅ **协作式**取消 | Lancer 是杀进程，墨溯不能崩 GUI |
| `runtime-journal` 的凭证脱敏 | ✅ 采集 + 替换两步 | — |
| Unix socket（明确拒绝 Windows） | ✅ localhost TCP + token | Windows 是主战场 |
| journal 不截断 | ✅ **有界保留 500 条** | 墨溯是长驻服务，不截断内存会一直涨 |

### 三个设计判断

**① seq 是全局的，不是每会话独立。**
分会话编号会让人以为 seq 在会话之间可比，然后写出错误的过滤条件。

**② 凭证脱敏必须两步。**
只做替换会漏掉「字段名叫 `notes`、值其实是 key」的情况；
只做采集不替换则文本里仍有明文。所以：先从
`key|token|secret|password|authorization` 命中的字段**采集**，
再把采集到的字面量从所有文本里替换成 `[REDACTED]`。

**③ 取消是协作式的，不打断进行中的工具调用。**
打断会留下半截状态（工具写了一半、事务开着）。
做法是在 ReAct 循环的**下一步**检查取消标志——所以 `stop` 会等一小会儿确认它真停了。
等不到就返回 `stopped=false` + note 说明「已请求但还没停下」——
**撒谎说停了比说没停更糟**。

### 端到端抓到的 2 个真 bug

**Bug 1：`stream` 一空闲就断开 → 「先订阅、后派活」整条能力作废。**

原实现：没新事件且当前没任务在跑 → 立刻 `break` 收尾。
问题：外部程序最自然的用法是**先开一条订阅等着，再派活**——
可它一连上就发现「当前没任务在跑」→ 立刻断开 → 派活时已经没人接了。
真机上就是「订阅端收到 0 条事件」。

修法：引入 `idleTimeout`（默认 30 秒），空闲要**连续一段时间**才算真空闲。
不是照抄 Lancer 的常驻语义——墨溯是长驻服务，不能让每条订阅连接无限期挂着。

**反向验证**（不能只看测试绿）：把服务端改回旧逻辑 →
新增的两条单测**双双失败**；恢复修复后双双通过。

**Bug 2：GUI 侧的洞察事件进不了 journal → 订阅端看不到用户在 GUI 里问的问题。**

根因：GUI 的 `/api/insight/ask` 走 GUI 自己的 TaskHub，
派活通道走 `InsightRunner._start`——两条路各写各的，
`EventJournal` 只被后者碰过。

而 **GUI 才是墨溯的主入口**。不补这条，`stream` 就只在边角场景可用。

修法：`InsightRunner` 开三个公共方法（`note_run_started` / `note_step` /
`note_run_finished`），两条路**共用一本日志**；
GUI 侧额外用 `mark_done` / `alias_run` / `rename_done`
让 GUI 那一轮的轮次也能被 `wait` / `tail` 按 runId 查到
（落盘给 id 加后缀时，事件归属与状态要一起改名）。

### 实测记录

- 新增单元测试 **68 例**（`tests/test_dispatch_stream.py`）+ 既有 `test_dispatch.py` 46 例
- 端到端 **38/38 通过**（真起 GUI、真发 HTTP、真模型、真收字节）
- 全量回归 1092 → **1160 passed / 16 skipped / 0 failed**
- 验证报告 L5（`docs/verification/iter9-dispatch-stream.json`），10 条「哪些没跑」

真机跑出来的关键事实：

| 场景 | 实际结果 |
|---|---|
| 先订阅后派活 | 收到 6 条事件，seq=[1..6] 严格递增 |
| `since` 游标 | since=4 只收到 [5,6]，不重复旧事件 |
| `tail` | lastAssistant 233 字，含「没有张三这个人」的诚实回答与依据引用 |
| `stop` 真打断 | busy=True → stopped=true → `wait` 看到 `cancelled`（不是干等到超时） |
| 空闲时 `stop` | 明确报 `NOT_CANCELLABLE`，不谎报 |
| 鉴权 | token 不对 → HTTP 401 |
| 脱敏 | 事件流全文扫不出真 API Key 明文 |
| GUI 侧洞察 | 订阅端收到 25 条事件（11 条 toolFinished），`wait`/`tail` 都能按 runId 查到 |

---

## 六之五、迭代 10 详细记录：证据账本（来源 / 口径 / 推断）

### 读 Lancer 读出了什么

Lancer 的证据账本在 `apps/desktop/contracts/desktop-state.ts:120-161`，四个维度：

| 维度 | 取值 |
|---|---|
| `OrchestrationEvidenceKind` | `worker_report` / `orchestrator_acceptance` / `orchestrator_observation` / `orchestrator_action` / `command` / `review_finding` / `blocker`（7 种） |
| `OrchestrationEvidenceSource` | `worker-reported` / `orchestrator-accepted` / `orchestrator-observed` / `orchestrator-action` / `command` / `review` / `blocker`（7 种） |
| `OrchestrationEvidenceStatus` | `reported` / `accepted` / `running` / `passed` / `failed` / `blocked`（6 种） |
| `severity?` | `P0` / `P1` / `P2` / `P3` |

产生点有 5 个（`app-store-orchestration.ts`）：`evidenceFromChildTranscript`(1806)、
`commandEvidenceFromTool`(1845)、`evidenceFromParentTranscript`(1866)、
`explicitAcceptanceEvidenceFromParentMessage`(1929)、
`blockerEvidenceFromChildStatus`(1959)。

最值得抄的是**它怎么拿到 severity**：不是模型自报，而是程序化从文本里正则提取
（`severityFromText`(2004)：`/^\s*\[?(P[0-3])\]?\s*:/`；`looksLikeBlocker`(2000) 同理）。
**「分级由程序判，不由模型说」**——这个思路墨溯直接采纳。

### 抄了什么 / 没抄什么

| Lancer 的 | 墨溯的 | 为什么 |
|---|---|---|
| `source` 7 种（编排语义） | `source` 4 种：`db` / `message` / `agent` / `model` | Lancer 那 7 种回答的是「这条证据在多 agent 编排链里的位置」。墨溯分析聊天记录、**没有编排验收流程**，照抄会得到 7 个永远填不满的枚举值。墨溯真正要回答的是「从哪来的、能不能核验」 |
| `status` 6 种生命周期 | `status` 2 种：`verified` / `reported` | Lancer 的 `reported→accepted→running→passed/failed/blocked` 是为**多 agent 验收**设计的。墨溯照抄会得到一个永远是 `reported` 的死字段 |
| `severity`（P0–P3） | **刻意不抄** | 那是「代码审查发现的问题有多严重」。聊天分析里没有对应物，硬套是形式主义。已写成单元测试 `TestNoSeveritySneakedIn` 锁死，防止后来者顺手补上 |
| `severityFromText` 程序化分级 | **`scope` 口径四字段**（Lancer 没有，墨溯自加） | 「消息量 158565 条」用户没法核对——是全库还是某会话？哪段时间？哪个维度？没有口径的数字，用户只能选择信或不信 |
| 数字审计单池 | **分池**：主代理实测 vs 子代理转述 | 子代理的数字有出处但转了一道手，提示等级该不同 |
| —— | **`inferences` 推断标注**（Lancer 没有） | 模型的主观判断（「他可能在疏远你」）必须跟「查出来的事实」分开摆 |

墨溯真正补齐的 4 件事 + 1 条契约：
1. `source`（来源区分）
2. **`scope`（口径）** —— 统计对象 / 会话 / 时间范围 / 统计维度
3. 数字审计**分池**
4. **推断标注** —— 程序化检测推断措辞，不让模型自报
5. 新 forbidden 契约 `no_stat_without_scope`（数字证据必须带口径，可机械检测）

### 三个设计判断（写下来防止后来者"顺手抄回去"）

1. **`source` 按墨溯语义重映射为 4 种** —— 不抄 Lancer 的编排语义。
2. **`status` 只有 2 种** —— 不抄 Lancer 的 6 种编排生命周期。
3. **`severity` 刻意不抄** —— 聊天分析里没有 P0–P3。

### 端到端抓到的 5 个真 bug（都是单测抓不到的）

| # | 现象 | 根因 |
|---|---|---|
| 1 | 场景 A 全灭，「洞察跑出答案」失败、`steps=0`、证据 0 条 | `/api/insight/ask` 是**异步**的（立即返回 `{taskId, status:"running"}`），脚本按同步接口读。**这是异步契约，不是 bug** |
| 2 | 场景 I「没拿到 runId」 | 历史回看接口路径写错（用了不存在的 `/api/insight/run/{id}`，实际是 `/api/runs/detail?id=`） |
| 3 | 场景 A「产出了证据」失败（0 条） | `t_find_contact` / `t_list_sessions` **返回了数字却不产出证据**——答案里的「50 个会话」在证据池里查不到出处，正是本轮要治的病 |
| 4 | 场景 D「quote 带原文全文」196/212 | **非文本消息（表情/图片/语音）正文在库里就是空的** → 产出**空白证据**，用户点开什么也看不到，「可核对」成了空话 |
| 5 | 场景 F「推断标注」始终为空 | **推断词表在真实答案上整轮漏检**。模型实际写的是「整体是」「这也解释了」「这是典型的」「我的判断」——第一版词表 12 个词一个都不覆盖 |

第 4、5 两条最值得记：**它们都是「能力看着有、实际不工作」**。
第 5 条还额外暴露了判据设计错误——第一版把「你说一下大概是什么内容」误标成推断（那是澄清提问，不是判断）。
**错标比漏标更伤**：错标告诉用户「这是模型的猜测」，但模型只是在正常反问。

修法：词表按**真实答案样本**重写并放宽（短词、不带位置约束），引号规则改成
「引用动词必须紧挨引号才算引原话」，再加「疑问句不标」兜底。

### 实测记录

- 单元测试：48 例新单测全绿；全量回归 **1225 passed / 17 skipped / 0 failed**
- 端到端：**54/54 通过**（`tests/e2e/lancer/e2e_iter10.py`，两轮真模型 + 真数据）
- 验证报告 L5（`docs/verification/iter10-evidence-ledger.json`），10 条「哪些没跑」

真机跑出来的关键事实：

| 场景 | 实际结果 |
|---|---|
| 两轮洞察 | 第一轮统计（steps=2，1 条 db 证据），第二轮检索（steps=9，176 条证据含 174 条 quote） |
| source 覆盖 | `['db', 'message']` 两种并存 |
| 口径随条件变 | `period` 从「全部时间」→「2025-01-01 00:00 ~ 2025-06-30 23:59」 |
| quote 可核验 | 174/174 条带原文全文 + 定位信息（会话/时间/发送者） |
| 推断标注 | 第二轮标出 4 条，含模型自报的「要说判断：这不是闹别扭的那种冷淡…」 |
| 不误标 | 事实句「10-22 08:45 他说我基本上都没去」不标；澄清提问不标 |
| 落盘 | 第二轮 inferences 落盘一致（1 vs 1），87 条 quote 带全文 |

---

## 六之六、迭代 11 详细记录：子树结算 + 回执协议

### 起点：墨溯的「结算」是空的

迭代 11 开始前，`core/agent_team.py` 里有两样东西：

- `settle_children()` —— 定义了，**从未被调用**
- `RECEIPT_ACCEPTED` —— 常量定义了，**一次都没被赋值过**

更糟的是 `settle_children` 上方的注释写着「子代理本身不派孙分支」——
**那是错的**。`_sub_delegate` 真的能派孙分支（孙层拿得到 `delegate_tasks` 工具），
`can_delegate(depth)` 控制的是深度上限。

也就是说：「父级核对」和「子树结算」这两件事在代码里**根本不存在**，
只是能力矩阵上写着「有」。

### 读 Lancer 读出了什么（第一遍读错了，第二遍才对）

Lancer 的相关代码分散在四个文件：

| 文件 | 位置 | 作用 |
|---|---|---|
| `orchestration/branch-return.ts` | 29 | 子分支跑完 → 产出一条 `worker_report` 证据，`status="reported"` |
| `application/app-store.ts` | 3586 | 报告**确实送达父会话**后 → 状态翻成 `"accepted"` |
| `orchestration/branch-return-batch.ts` | 29-39 | `settled()` 的三层判据 |
| `orchestration/app-store-orchestration.ts` | 1929 | `explicitAcceptanceEvidenceFromParentMessage`，扫父文本找 `orchestrator-accepted:` |

Lancer 的 `settled()` 原文判据是**三层**：

```ts
const settled = (child) => {
  if (child.supervisionLoop?.gate === "stop") return true;
  if (seen.has(child.id) || !["complete", "failed"].includes(child.status)) return false;
  return childrenOf(children, {...}).every(
    (c) => settled(c) && (c.supervisionLoop?.gate === "stop" || pending(c).length === 0),
  );
};
```

其中 `pending(c)` = `c.evidence.filter(e => e.toolName === BRANCH_RETURN_TOOL && e.status === "reported")`。

**第一遍我读错的地方**：我以为结算门槛是「父代理在文本里写接受标记」。
照着这个理解实现，会得到一个**永远为假**的结算——因为 Lancer 全仓
**没有任何提示词**教模型写 `orchestrator-accepted:`（只有 3 处代码引用，0 处 prompt）。
照抄成门槛 = 把能力押在模型自觉上 = 白实现。

**第二遍读对的结论**：Lancer 的 `accepted` 是**投递确认**（报告送到了），
`orchestrator-accepted:` 是**平行的另一条证据源**（父级明确表态）。
两者不是一回事。

### 抄了什么 / 没抄什么

| Lancer 的 | 墨溯的 | 为什么 |
|---|---|---|
| `settled()` 三层递进 | `branch_settled()` 同款三层 | 直接抄，这是判据不是风格 |
| 送达确认（`app-store.ts:3586`） | `consume_branch()` 在 **payload 回喂进 messages 之后**才标 | 送达点必须真在回喂之后。早标 = 自己给自己签字 |
| 回执由**直接父**消费 | 孙分支的 `consumedBy` 是它的直接父 id，**不是 main** | 混成 main 就跳过了「直接父消费」这一环，语义就废了 |
| `orchestrator-accepted:` 文本标记 | `mosu-accepted:` 同样程序化正则提取 | 抄思路（迭代 10 的「分级由程序判」延续） |
| — | **显式接受不参与结算判定** | Lancer 自己都没这么干。照抄成门槛 = 结算永远为假 |
| — | **前端措辞「已回传」而非「已核对」** | 「核对」是人的动作，程序只能说「送到了」 |

### 关键设计：送达 vs 采信是**两件事**

```
receipt = reported → accepted      程序判定：返回确实进了上层的上下文   ← 结算门槛
acceptedBy / acceptReason          模型表态：父级写了 mosu-accepted:   ← 加分信息
```

混成一个字段的后果是「送达即采信」——用户看到「已核对」，
实际只是「送到了」，中间差着一次真正的核对。分开摆才是诚实的。

单元测试 `test_acceptance_does_not_affect_settlement` 把这条钉死：
**显式采信了但没送达 → 仍然不结算**。

### 端到端抓到的真问题

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | `settlement` 落不了盘 | `gui/server.py` 的 `save_run` 调用没带这个字段（跟迭代 10 的 `inferences` 一模一样的坑） | 补字段 + 端到端反查 |
| 2 | 徽标 title 恒显示「直接父」 | `flatten()` 没把 `acceptedBy` 传进 node，模板里 `n.acceptedBy` 恒 undefined | 补传字段（前端渲染测试抓到的） |
| 3 | 结算判定漏掉「父分支自己没跑完」 | 初版用「pending 为空」当 settled 判据，父分支 running 但子分支都已送达时会误判为已收敛 | 改用 `settle_children()` 的三层判据 |

### 端到端实测结果（真引擎 + 真数据 + 脚本化 LLM 决策）

| 项 | 结果 |
|---|---|
| 三层委派 | 9 条分支（3 顶层 + 6 孙层），`Branch.depth` 达 2 |
| 送达状态 | 9/9 全部 accepted，无 pending |
| 送达方归属 | 孙分支的 `consumedBy` 是直接父 id，非 main |
| 送达时序 | 主代理第二次调用时上下文已有 1 条 tool 结果；子代理上下文也有孙分支结果 |
| 结算 | `settled=True`，total=9，consumed=9 |
| 未结算报警 | 构造未送达场景 → 答案里出现「⚠️ 分支未结算」并点名分支 id |
| 落盘 | `settlement` 字段进 `~/.mosu/runs/*.json`，历史回看可读 |
| 前端渲染 | 17 例全绿（真浏览器 + 真实组件 + 固定数据） |
| 全量回归 | 1262 passed / 17 skipped / 0 failed |

**为什么用「脚本化 LLM 决策」而不是纯真模型**：真模型不保证会派孙分支——
它可能一层都不派。那样测出来的「结算通过」是**碰巧**，不是能力。
所以只替换模型的「决定调哪个工具」这一步，其余全走真实路径：
真 Dataset / 真 ToolBox / 真 AgentTeam / 真 ReAct 循环 / 真 settle_children。
核心逻辑一行没改，固定住的恰恰是端到端该固定的那部分。

### L5 报告

`docs/verification/iter11-branch-settlement.json`（15 条 claims + 7 条 not_run）

## 六之七、迭代 12 详细记录：跨分支语义锚点光线

### 起点：墨溯的分支树只有「谁派的谁」

迭代 4 抄来了 Lancer 的**树线**（父子关系）+ 泳道分列，
但 Lancer 的树上其实有**两类线**：

| 线 | Lancer 的来源 | 说的是 |
|---|---|---|
| 树线（实线） | 父子关系 | **谁派的谁**（编排关系） |
| 语义边（虚线） | `event.reference` / `event.retryTarget` | **这两条线指向同一件事**（内容关系） |

墨溯只有第一类。用户看到的分支树里，
「三个子代理都在查同一个人」这件事**完全看不出来**——
三根平行的线，看不出它们其实在查同一件事。

### 读透的 Lancer 文件

| 文件 | 读了什么 | 抄了什么 |
|---|---|---|
| `process-curve-routes.ts`（201 行） | `processTreeRoutes()`（持久语义边）vs `processCurveRoutes()`（瞬态飞行曲线）；`readCurveAnchor(root, key, origin)` | 端点绑**语义标识**而非坐标；读不到锚点返回 `null` |
| `process-tree.tsx:441` | `strokeDasharray={route.kind === "retry" ? "2 2" : "4 4"}` | 虚线（视觉上与实线树线分开） |
| `process-types.ts` | `ProcessEvent` 的 `reference` / `retryTarget` 字段 | 判据的**来源** |

Lancer 原文注释（抄下来了，写在代码里当依据）：

> `No coordinate-based pairing; a clipped anchor cannot be replaced by a nearby node.`
>
> `The live shell hides the authored relay strip. Its visible lane-head model carries the same semantic key; never let a hidden duplicate mask that chip.`

### 🔴 第一遍读错了：差点把「模型写标记」当成判据

Lancer 的语义边来自**事件流里的显式引用**——
`event.reference` 指向另一个事件。墨溯**没有等价的东西**：
子代理之间不会互相引用事件。

所以第一版我下意识地想抄成「模型在结论里写 `mosu-referenced: <分支id>`」，
然后让程序去解析这个串当判据。

**这是迭代 11 刚踩过的同一个坑**：Lancer 全仓 3 处代码引用
`orchestrator-accepted:`，但**0 处提示词教模型写它**——
照抄成门槛 = 永远为假 = 白实现。

所以这一轮明确：**抄纪律，不抄判据**。

### 墨溯自己的判据（全部程序可算，不靠模型自报）

| 判据 | 依据 | 为什么是它 |
|---|---|---|
| `shared-session` | 两个分支的 **quote 类证据**里出现同一个 `sessionId` | 都真读到了同一个人的原话 |
| `shared-topic` | 两个分支 **目标文本**（goal）里共同的话题词（2 ≤ 重叠 ≤ 8） | 同一件事的不同角度 |

**为什么 shared-session 只看 quote 类证据**（第一版真实踩到）：
`find_contact` 会为**搜到的每个会话**产一条 stat 证据——
那只说明「搜到了这个人」，不是「读了这个人的消息」。
把 stat 算进去 → 「查了张」的分支和任何分支都连上 → 满屏假线。

**为什么砍掉第三种判据 `time-overlap`**：
并行委派下三个子代理两两时间都重叠 → 每对都连 → 纯噪音。
噪音会稀释真正有信息的线，用户看多了就不信这层标记了。
**假关系比没线更糟。**（砍掉的理由写进代码注释，防止后人加回来）

**为什么标签用人名不用 sessionId**：
sessionId 是 `wxid_xxx` / 群 username，用户看不懂「都查过 wxid_xxx」。
标签取 quote 证据里的 `sender`。

### 墨溯与 Lancer 的差异（有意为之）

- Lancer 语义边来自事件流显式引用 → 墨溯无等价物 → **判据全换成自己的**
- Lancer 语义边画在**树的可滚动坐标系**里 → 墨溯是静态布局，
  一次算 + rAF 持续重算（有脏检查，不产生重渲染）
- Lancer 的语义边在**树的同一层** → 墨溯把语义边**单独一层、压在节点上面**
  （语义边横跨泳道，压在下面会被卡片盖住看不见，hover 也命中不到线）

### 端到端抓到的真问题

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | **孙分支在树里出现两次** → 前端画重复节点 | `Branch.__init__` 已经 `parent.children.append(self)`，`agent_team` 拿到二次委派结果后又 `br.children.extend(kids)` | extend 前按 id 去重（**迭代 11 就存在的 bug，迭代 12 才抓到**） |
| 2 | 语义边自环（`from == to`） | 构造 `Branch(parent=P)` 会自动挂进 `P.children`，摊平整棵树时同一分支出现两次 | `_all_branches()` 按 id 去重 + 自环显式跳过 |
| 3 | `attach_semantic_links(None, None)` 崩 | 没防 None | `links or []` |
| 4 | 语义线 hover 不触发 | 连线层 `pointer-events:none` + 语义边横穿节点卡片 → 命中的是卡片不是线 | 语义边拆成**独立图层压在节点上面** + 单独的宽命中区路径 |
| 5 | 语义边只按 stat 证据就连上 → 满屏假线 | 第一版 `_branch_sessions()` 取了所有证据的 sessionId | 收紧为只取 `kind == "quote"` |

### 端到端实测结果（真引擎 + 真数据 + 脚本化 LLM 决策）

| 项 | 结果 |
|---|---|
| 三层委派 | 4 条分支（3 顶层 + 1 孙层），真 AgentTeam + 真 ReAct 循环 |
| quote 证据 | 3/4 个分支真读到了原话（`read_timeline` → `kind="quote"`） |
| 语义边 | 2 条 `shared-session`，标签「都读过「赵小刚」的聊天」 |
| 判据不误连 | stat-only 分支**没被 shared-session 连上** |
| 无自环 / 无父子边 | ✅ |
| 落盘 | `semanticLinks` 进 `~/.mosu/runs/*.json` |
| GUI 下发 | 分支 dict 带 `semanticLinks`，字段齐全（from/to/kind/label/color） |
| 前端渲染 | 12 例全绿（真浏览器 + 真实组件 + 固定数据）+ 端到端真浏览器渲染真数据 |
| 全量回归 | 1296 passed / 17 skipped / 0 failed |

**为什么用「脚本化 LLM 决策」**：真模型不保证会派两个子代理去读**同一个人**——
那样测出来的「语义边成立」是**碰巧**，不是能力。
只替换「模型下一步调哪个工具」这一步，其余全走真实路径。

脚本派发也有个坑：一开始按「第几次调用」派脚本，
并行子代理的创建顺序不确定 → 竞态 → 每次跑结果可能不同。
改成按 **goal 文本**匹配规则派发，与线程调度无关。

### L5 报告

`docs/verification/iter12-semantic-edges.json`（12 条 claims + 5 条 not_run）

---

## 六之八、迭代 13 详细记录：补迭代 6/7 的未跑项（并抓到 5 个真 bug）

### 起点：报告里的 not_run 不是「待办清单」，是**能力缺口清单**

迭代 6 和迭代 7 的 L5 报告各留了 9 条 not_run。当时我以为那些都是
「测试还没写」，直到这一轮逐条去看，才发现有**一半是真功能缺口**：

| not_run 项 | 表面像 | 实际是 |
|---|---|---|
| 启用名单的 UI 入口 | 测试缺口 | **功能缺口**：后端接口有、前端零入口，没人能从界面设置 |
| CLI 侧的模型目录命令 | 测试缺口 | **功能缺口**：迭代 6 只做了 GUI，CLI 一个命令都没有 |
| 契约判定失败前端不白屏 | 测试缺口 | **功能缺口（且静默）**：异常被吞掉，用户以为答案经过校验 |
| 历史回看渲染阶段契约 | 测试缺口 | **功能缺口**：契约早落盘了，界面从不渲染它 |
| 多代理分支的阶段归类 | 测试缺口 | **功能缺口（误判）**：成功的多代理回答被判成「没通过」 |

**为什么一开始看不出来**：这些缺口的共同点是——
「代码看起来都在」。`/api/models/enabled` 存在、`pipeline.evaluate` 有 try/except、
`record.contract` 有落盘。**存在 ≠ 可用**。
只有真跑一遍用户路径，才会发现「后端做了、前端没接」和「异常被吞了」。

### 🔴 端到端抓到的 5 个真 bug

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | 切换模型后「当前模型」徽标退回 `?` | `api_models()` 手工塞 `current`，而 `activate`/`endpoint_save` 返回的 catalog 来自 `load_model_catalog()`（不含 `current`）→ 前端用返回值整体替换本地状态后字段丢了 | 把 `current` 组装**收敛**进 `load_model_catalog()`，所有返回 catalog 的接口字段天然一致 |
| 2 | 徽标 bug 从来没人看见 | `mode="bar"` **从未在任何页面挂载**（只有 settings.js 挂了 `mode="full"`）→ 那套「当前模型徽标」代码在真机上从来没渲染过一次 | full 模式也显示当前模型条（作为状态指示，不响应点击展开） |
| 3 | `e2e_iter7.py` 的回归测试**一直在失败** | 它还写死 `len(stages) == 4`，而迭代 8/10 已加到 5 阶段 / 6 条 forbidden → 没人跑它所以没人发现 | 改成从 `core.pipeline.STAGES` / `FORBIDDEN` 读，不写死数字 |
| 4 | 契约判定失败**完全静默** | `except: run.contract = {}` 吞掉异常 → 「契约判定炸了」和「这轮没做判定」在界面上完全一样 | 兜底成带 `error` 的结构 + 前端显式提示「验收环节未生成」；并把判定抽成模块级 `_evaluate_contract()`，让端到端能**真打坏产品代码**再验兜底 |
| 5 | **多代理分支下阶段归类误判**（最严重） | 阶段判定只看**主代理的工具调用**。多代理委派时主代理只调 `delegate_tasks`（归到 orchestrate 阶段），数据是**子代理**查的 → `probe` 判 MISSING、`read` 判 SKIPPED、`okToProceed=False` → **一次成功的多代理回答被判成「没通过」** | 判据改成「调用过 **或** 有对应证据」；并反向验证「委派了但啥也没查到 → 仍判 MISSING」，门禁没放松 |

**第 5 条的方法论价值最大**：它证明**判据的设计必须对齐「谁在干活」**。
原判据默认「主代理干所有活」，那是单代理时代的假设。多代理一上来就崩。
修法不是「放宽门槛」（那会真的放过没查证的回答），
而是「判据问对了问题」——阶段关心的是「有没有拿到东西」，不是「谁拿的」。

**第 3 条是本轮最刺眼的一条**：一条**一直失效的回归测试**比没有更糟——
它给人「已覆盖」的错觉。这正是「存在 ≠ 可用」在测试侧的同一个病。

### 补上的三块功能

| 块 | 补了什么 | 端到端断言 |
|---|---|---|
| A 组（迭代 6） | 启用名单 UI 入口（后端 GET 读接口 + 前端 textarea + 两个按钮）；连真网关跑通探测成功分支；同 id 双端点徽标归属（**在浏览器里点**）；切换后真发推理 | 35 条全绿 |
| B 组（迭代 7） | 契约判定失败不白屏 + 显式提示；历史回看渲染契约（抽出 `ContractStrip` 组件，当前轮次与历史回看**共用同一份模板**，从结构上杜绝漂移）；forbidden 第 4 条真实语料召回率；多代理阶段归类 | 38 条全绿 |
| C 组（迭代 6 末项） | **`mosu models` 子命令组**：list / endpoints / use / enabled / probe / endpoint add\|rm | 42 条全绿 |

**C 组的设计取舍**：argparse 的位置参数按顺序分配，`nargs="*"` 又只能放最后，
所以「use 要两个位置参数、enabled 要 N 条规则、endpoint 要动作+ID」
三种形状没法用同一套位置参数表达。与其塞 5 个语义重叠的可选位置参数，
不如固定两个槽位（`arg1`/`arg2`）由子命令自己解释——
argparse 只负责把字符串收进来，语义解释权交给命令函数。

**C 组还修了一个自己的原则矛盾**：我写了「破坏性操作必须显式」，
但实现上「不带 `--all` 的空跑」还是会清空名单。
端到端第一条失败就抓到了它——**测试抓的是实现的错，不是实现的形**。

### 端到端实测结果

| 项 | 结果 |
|---|---|
| A 组 | 35/35（真网关 38 个模型、真浏览器、真推理） |
| B 组 | 38/38（真打坏产品代码、真浏览器 mock、真库真工具收 48 条 quote 证据） |
| C 组 | 42/42（真 CLI 子进程、真 GUI 服务同源验证） |
| 全量回归 | 1279 passed / 17 skipped / **0 failed** |
| 契约真实语料 | 48 条 quote + 6 条 stat 证据；forbidden 第 4 条触发率 4/9 |
| 已知盲区 | 第 4 条不校验引号内容是否真在证据里 → 伪造引用查不出来（**漏报**，非误报） |

### 一条值得单独说的：为什么 C3.3 第一版失败

C3.3 断言「被启用名单过滤的模型要标成『未启用』」，第一版红了。
看着像产品 bug，其实是**测试构造错了**：我加的两个端点都没配 Key，
于是三态判定按优先级全落到「未配置」——`build_catalog` 里
`STATE_UNCONFIGURED` 的判断在名单过滤**之前**。

**这才是对的**：端点没 Key 时，模型本来就没法用，
标「未启用」会误导用户去改名单（改了也没用，因为缺 Key）。
补上 `--api-key` 后测试就绿了。**测试失败不总是产品的错**——
但每次都得先判清楚是哪边的错，不能默认往产品上猜。

### L5 报告

`docs/verification/iter13-not-run-backfill.json`（14 条 claims + 9 条 not_run）
迭代 6 / 7 的报告里补了 `resolved_in_iter13` 段，逐条记清怎么补的、证据在哪。

---

1. **真实数据内核**：SQLCipher4 自研解密、跨分片去重、图片 V2 私有容器解密、SILK 语音转写——
   Lancer 完全没有这一层。
2. **纯只读 + 零依赖 + 零网络**：Lancer 依赖 Electron、pnpm、模型服务。
3. **隐私敏感场景**：个人微信数据必须本地处理，Lancer 的架构假设数据在会话 JSONL 里，不涉及这类合规压力。

**结论：墨溯不需要变成 Lancer。墨溯需要 Lancer 的编排能力，来放大自己的数据内核。**
