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
| 9 个模型家族目录 | ❌ | 先支持 1 个模型 + 自定义端点够用；模型目录是 P2 |
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
| **回执协议** | `reported → accepted`，父级显式接受 | 无 | 缺 | **P0** |
| **预算准入** | 4 维（并发/深度/节点/token），首个 await 前占位防超订 | 单维 MAX_STEPS=8 | 缺 | **P0** |
| **分支落盘追溯** | `.lancer/branch-reports/<sha>.json` + 重启重建 | 无（`AgentRun` 只在内存，任务淘汰即失） | 缺 | **P0** |
| **子树结算** | 孙分支须先被直接父消费，才向上一级回传 | 无 | 缺 | P1 |
| **证据账本** | 8 种 kind + severity + git 快照 | `evidence` 有 `quote` / `stat` 两类 | 部分有 | P1 |
| **流式反馈** | 事件溯源 + callId upsert + 50ms 节流 | 900ms 轮询 `/api/task` | 差距明显 | P1 |
| **统一订阅通道** | 单条 `state-changed` 广播全量快照 | 每端点独立轮询 | 缺 | P1 |
| **外部派活通道** | Control API（Unix socket + token，`send`/`queue`/`steer`/`wait`/`tail`） | MCP 只读：外部能**读**墨溯，墨溯不能被外部**驱动** | 缺 | P1 |
| **多模型目录** | 9 家族 + 自定义 + 可用/未启用/未配置三态 | 单一 `LlmClient` | 缺 | P2 |
| **阶段流水线** | 1–8 阶段，各配 model/skills/goal/deliverables | 无 | 缺 | P2 |
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

- **多模型目录**：9 家族 UI 是大工程，墨溯先支持单模型 + 自定义端点。
- **阶段流水线**：墨溯的「分析流程模板」价值一般，等分支树跑通再考虑。
- **AgentMail / 订阅账号**：走 OpenAI 兼容端点已够。

---

## 六、迭代路线

| 轮次 | 内容 | 验收 |
|---|---|---|
| **1** | `core/agent_team.py`（分支树 + 预算 + 并行子代理 + 回执）；`core/locks.py` 全局数据锁；`delegate_tasks` 工具接入 `agent_engine`；洞察页展示分支卡片 | 真模型 + 真数据跑通「分别分析 3 个人」；数字审计覆盖子代理数字；回归全绿 |
| **2** | 洞察事件流 SSE 通道，替代 900ms 轮询；分支树落盘 `~/.mosu/runs/`；洞察历史可回看 | 前端实时看到子代理进度；重启后分支树可重建 |
| **3** | 外部派活通道（本地 socket + token，`queue`/`steer`）；`mosu dispatch` CLI | 外部 CLI 能派活给运行中的墨溯 GUI，并等结果 |
| **4** | 分支可视化（树 → 语义锚点光线）；多模型目录 | 颜色按泳道继承；锚点缺失不乱飞 |
| **5** | 验证文档升级为 6 级证据分级 | 每份报告明确列「哪些没跑」 |

---

## 七、墨溯相对 Lancer 的独有优势（不可被 Lancer 追上）

1. **真实数据内核**：SQLCipher4 自研解密、跨分片去重、图片 V2 私有容器解密、SILK 语音转写——
   Lancer 完全没有这一层。
2. **纯只读 + 零依赖 + 零网络**：Lancer 依赖 Electron、pnpm、模型服务。
3. **隐私敏感场景**：个人微信数据必须本地处理，Lancer 的架构假设数据在会话 JSONL 里，不涉及这类合规压力。

**结论：墨溯不需要变成 Lancer。墨溯需要 Lancer 的编排能力，来放大自己的数据内核。**
