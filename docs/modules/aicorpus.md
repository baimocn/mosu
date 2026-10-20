# AI 对话语料

把你在各 AI 工具里敲的字，接进墨溯的语料库。

## 为什么值得单独做

微信语料和 AI 对话语料是两种东西：

| | 微信 | AI 对话 |
|---|---|---|
| 说话对象 | 人 | 机器 |
| 社交面具 | 有（要顾及关系） | 几乎没有（下指令、催进度、纠错） |
| 单条长度 | 短（实测中位 11 字） | 长（实测中位 29 字，P90 528 字） |
| 反映什么 | 我怎么跟人相处 | 我怎么思考、怎么提需求、怎么验收 |

后者对"理解一个人怎么做判断"价值更高，但它散落在各个 AI 工具的私有格式里，没有统一格式。

## 支持哪些来源

| 来源 | 路径 | 格式 |
|---|---|---|
| WorkBuddy | `~/.workbuddy/projects/**/*.jsonl` | `type=message` + `role` + `content[]` |
| Claude Code | `~/.claude/projects/**/*.jsonl` | `type=user/assistant` + `message.content` |
| iFlow | `~/.iflow/projects/**/*.jsonl` | 同 Claude（同一套 sdk） |
| Reasonix | `~/.reasonix/sessions/*.jsonl` | `role` + `content`（字符串） |

会自动探测，目录不存在的直接跳过——不返回空来源凑数。

> 实测本机 `.workbuddy/projects` 有 111 个 jsonl / 686MB，是语料主库。

## 用法

```bash
# 只看报告，不写盘
python cli/mosu.py aicorpus

# 写成语料文件
python cli/mosu.py aicorpus --output persona/ai

# 只取最近 20 万字符（画像用，避免太重）
python cli/mosu.py aicorpus --max-chars 200000
```

界面：画像页 →「AI 对话」标签。接口：`GET /api/aicorpus`。

产出结构（与 `core/persona.py` 的微信语料对齐，下游可共用）：

```
ai_corpus.jsonl        全量，每条含 ts/source/project/role/text
by_project/*.txt       按项目分段的纯文本
```

## 最容易做错的地方：把系统的话当成你说的话

这是整个模块的核心难点。一条 `role=user` 的消息里，可能同时装着：

- 系统注入的环境信息（`<system-reminder>`）
- 系统注入的模式说明（`<craft_mode>`）
- 后台任务完成通知（`<task-notification>`）
- 队友 agent 生成的报告（`<teammate-message>`）
- 定时任务调度器写的执行模板（`You MUST follow these steps in order:`）
- **你真正敲的那句话**（`<user_query>`）

如果提取逻辑写错，结果不是"少几条"，而是**把 AI 的程序员口吻当成你自己的说话风格**——画像直接失真，而且不会报错。

### 三条实测踩出来的规则

**1. 「取真实输入」必须排在「丢弃注入」之前**

`<system-reminder data-role="user-context">` 开头的消息里，同时包着环境信息和**真实的 `<user_query>`**（实测 2182 条）。

第一版把"整条是注入就丢"放在最前面，结果总量从 2002 条掉到 177 条——真实输入被一起扔了。

**2. 剥掉标签后，还有残渣**

`<task-notification>` 被剥掉后，末尾还挂着系统写给 AI 的
`Use the TaskOutput tool with task_id=…`。这段不是人说的话，实测污染了几百条。

现在用 `_clean_residue()` 再兜一层。

**3. 短输入不能按长度过滤**

`<user_query>` 里的内容是你亲手敲的，哪怕只有「继续」「A」「B」也是真实输入。
实测 **301 条被 `MIN_LEN=8` 误杀**（含「请继续执行任务」×50、「在吗」「探一下」）。

短指令本身就是信号——说明这个人倾向下短语而不是写长需求。过滤掉会把"真实说话长度"整体拉长。

所以两个阈值分开：`MIN_LEN_TRUSTED=1`（标签内的，非空即可）、`MIN_LEN=8`（剥注入后的残渣，短的多半是噪音）。

### 压缩摘要单独标记

`<conversation_history_summary>` 是 AI 写的上下文摘要，**不是你的原话**，但它信息密度最高（浓缩了你几轮的意图）。
所以保留、标为 `role=summary`，不混进 user 语料。

## 能得出什么

```
单条中位长度 29 字，P90 528 字
模糊表达占比 3.7%（越低越倾向说清具体要求）
让我做 148 (11.1%) / 催进度 78 / 改错返工 61 / 追原因 45 / 授权放权 21
最活跃时段 20:00，凌晨 0-6 点占 12%
```

**只用措辞判定，不做性格推断。** 这和 `docs/modules/psych.md` 同一条纪律：看不清就留空，不猜。

## 已知局限

- **项目名有损**：目录名把 `:` `\` `/` 都换成 `-`，无法精确还原，只能去掉盘符段。不猜 `--` → `/`，因为连字符本身也可能是项目名的一部分。
- **不含 AI 的回复**：目前只收你说的。AI 的话是镜像，不是你的风格；但将来要做"我和 AI 如何协作"，那部分才需要。
- **图片/附件占位符已剥离**：`@image#1:xxx.png` 对语料无意义。
- **接口首次约 6 秒**：要扫 686MB。已做的是界面明确等待，未做的是增量索引（数据每天都在长，值得做但需要落地文件状态表）。

## 相关

- `core/aicorpus.py` —— 实现
- `tests/test_aicorpus.py` —— 24 例，重点测"哪些必须被丢掉"
- `docs/modules/psych.md` —— 心理学维度（同一套"给证据不给结论"的纪律）
