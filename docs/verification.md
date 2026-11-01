# 验证纪律：6 级证据分级

> 对标 Lancer 的 `.agents/skills/verify-pi-gui/` + `docs/ci-baseline.md` +
> `docs/release-readiness.md` + `docs/platform-validation.md`。
>
> 本文档日期：2026-11-02 ｜ 校验器：`core/verify_report.py`

---

## 一、为什么需要这份文档

Lancer 的核心资产不是代码，是**一整套关于「什么才算验证过了」的纪律**。

它的 `AGENTS.md` 写着：

> Desktop work is not done until it is verified on the real Electron surface,
> not only by unit tests.

它的 `ci-baseline.md` 写着：

> A settings smoke does not establish a working conversation.

它的 `platform-validation.md` 里，Windows 一栏是这么写的：

> Build/test configuration is present. ｜ **No native Windows acceptance in this round.**

最后那半句才是重点：**它明确写出了「没验什么」。**

墨溯前四轮的迭代里我自己也踩过同一个坑：
端到端断言「隐藏节点后连线全部消失」在连线数为 0 时**空跑通过**，
把真 bug 完整盖住——单测全绿、文档齐整，但功能是坏的。
**问题不在于没测，在于不知道自己没测什么。**

所以这份文档定两件事：

1. **证据分几级**——说清楚「我这条证据到底有多硬」
2. **每份验证报告必须列出「哪些没跑」**——不能靠沉默蒙混

---

## 二、6 级证据分级

分级只看一件事：**这条证据离「用户在真机上真的能用」有多远。**

| 级别 | 名称 | 判据 | 墨溯的实例 |
|---|---|---|---|
| **L1** | 静态检查 | 只读源码/配置，**不执行**任何被测逻辑 | 模板编译校验、`test_templates.py` 的接线检查、AST 形态匹配 |
| **L2** | 单元测试 | 在进程内调用函数，**用固定数据**断言返回值 | `core/sqlcipher4.py` 的页布局解析、`core/msgparse.py` 的 protobuf 解包 |
| **L3** | 离线集成 | 跑**真实 I/O**（真文件/真 socket/真 SQLite），但数据是夹具 | `test_dispatch.py` 真开 socket 收发 HTTP；`test_branch_tree_render.py` 真浏览器渲染 |
| **L4** | 真实数据 | 用**用户真实数据**跑通功能链路（读自己的微信库） | `mosu stats` 在真实 23 万条消息上跑；图片解密 127/127 |
| **L5** | 真机端到端 | 走**真实界面**（浏览器/UI）+ 真实后端，用户可见的完整路径 | Playwright 真机点提问 → 洞察 → 分支树渲染 → 展开证据 |
| **L6** | 真机全链路 | L5 + **真模型** + 真数据 + **跨进程/跨会话**验证 | 派活 CLI → 墨溯跑完 → 落盘 → 重启 GUI 回看历史 |

### 判定的硬规则

1. **只声明达到的那一级**，不能往上够。
   「真机看了页面但没真提问」是 L3 不是 L5——界面渲染不等于功能可用。
2. **L4 不能替代 L6**。
   合成数据通过 ≠ 真实数据通过（Lancer 原话）。
3. **L2 通过 ≠ L5 通过**。
   单测全绿时完全看不出来——本项目迭代 3 的 CLI 退出码恒为 0，
   就是单测全绿、端到端才暴露的。
4. **L1 只证明「接线对」，不证明「行为对」**。
   迭代 4 的 Vue 组件注册方向反，`test_branch_tree.py` 的 L1 静态检查
   抓不到（它检查的是「文件里有这个字符串」），是 L5 真机暴露的。
5. **拿不到证据时，声明「没跑」，不要降级凑数**。

### 每一级各自能证明什么、不能证明什么

| 级别 | 能证明 | **不能**证明 |
|---|---|---|
| L1 | 代码结构接线对、纪律没被写坏 | 任何运行时行为 |
| L2 | 单个函数的输入输出对 | 模块之间接得上、真实数据格式对 |
| L3 | 模块之间接得上、I/O 通路通 | 真实数据下不炸、用户看得见 |
| L4 | 真实数据下功能跑通 | 用户能用（可能根本没有界面路径） |
| L5 | 用户在真机上能走通这条路径 | 换个会话/重启后还在、数据是假的 |
| L6 | 端到端真的能用 | 换台机器/换个人还能用（那需要 L6 复跑） |

---

## 三、每份验证报告必须写清「哪些没跑」

这是本文档最硬的一条。**报告没有「没跑」清单 = 报告不可信。**

报告格式（JSON，见 `core/verify_report.py`）：

```json
{
  "title": "迭代 4：分支树可视化",
  "level": "L5",
  "date": "2026-11-02",
  "commit": "af8301a",
  "claims": [
    "3 个顶层子代理各占一条泳道，颜色互不相同",
    "颜色继承到节点边框（孙节点继承父泳道色）",
    "隐藏一个节点 → 只有连到它的线消失，其他线不动"
  ],
  "not_run": [
    "多模型目录（Lancer 9 家族）——迭代 4 的另一半，未开始",
    "分支树在浅色主题下的配色对比度（只验了暗色主题）",
    "Windows 缩放 150% 下的曲线端点精度（只验了 100%）"
  ],
  "evidence": [
    ".cache/audit/v2/iter4-tree.png",
    ".cache/audit/e2e_iter4.py"
  ]
}
```

### 硬规则

1. **`not_run` 字段必须存在**，哪怕是空数组 `[]`。
   字段缺失 = 报告不合格（校验器直接拒）。
2. **「没跑」要具体到可执行**。
   ✅「Windows 缩放 150% 下的曲线端点精度」
   ❌「其他场景未验证」「可能有遗漏」
3. **`level` 不能高于 `claims` 里最强的那条证据**。
   声明 L6 但 `claims` 全是单测结果 → 校验器拒。
4. **写完报告再跑一次校验**：`python -m core.verify_report <报告路径>`

---

## 四、校验器

`core/verify_report.py` —— 纯标准库，零依赖。

```bash
# 校验单份报告
python -m core.verify_report docs/verification/iter4-branch-tree.json

# 校验整个目录
python -m core.verify_report --dir docs/verification/
```

校验器会拒掉这几种报告：

| 拒的原因 | 为什么必须拒 |
|---|---|
| 缺 `not_run` 字段 | 沉默 = 假装全验了 |
| `level` 不在 L1–L6 | 分级表是封闭的 |
| `level` 高于 `claims` 能支撑的强度 | 虚报证据等级 |
| `claims` 为空 | 一条都不证明的报告没有意义 |
| `evidence` 里的路径不存在 | 证据必须能打开 |
| `not_run` 里有「未验证」「可能有遗漏」这类空话 | 不可执行的不算「没跑」 |

---

## 五、墨溯的验证分层（现状）

| 验证面 | 当前级别 | 怎么跑 | 备注 |
|---|---|---|---|
| SQLCipher4 解密 | **L4** | `pytest tests/test_sqlcipher4.py` + 真实微信库 | 真实数据实测通过 |
| 消息解析 | **L4** | `pytest tests/test_msgparse_readable.py` | 23 万条真实消息 |
| 图片解密 | **L4** | `mosu media` | V2 私有容器 127/127 |
| 派活通道 | **L5** | `pytest tests/test_dispatch.py` + `.cache/audit/e2e_iter3.py` | 真 socket 真模型 |
| 洞察 Agentic | **L5** | `.cache/audit/e2e_iter2.py` | 真模型真数据 |
| 分支树可视化 | **L5** | `pytest tests/test_branch_tree_render.py` + `.cache/audit/e2e_iter4.py` | 真浏览器 |
| 微信通道（收发） | **L3** | `pytest tests/test_wechat*.py` | 未做真机收发端到端 |
| 语音 SILK→MP3 | **L3** | `pytest tests/test_media.py` | 真实转码可用，未做端到端 UI 验证 |
| 打包 / 分发 | **L1** | 无 | **纯 Python，pip 即用，无打包环节** |
| 多平台（macOS/Linux） | **L1** | 无 | **只在 Windows 开发，跨平台未验** |

---

## 六、当前已知「没跑」的清单（项目级）

这一节是**项目当前状态的诚实快照**，任何人都能拿它对照：

1. **多模型目录**（Lancer 9 家族）——迭代 4 的另一半，未开始
2. **浅色主题下的分支树配色**——只验了暗色
3. **Windows 缩放 ≠ 100%** 时的曲线端点精度
4. **macOS / Linux**——墨溯只在 Windows 开发过，跨平台零验证
5. **微信通道的真机收发**——协议层测过，真机会话没跑通
6. **`export/` 产物目录**——约 1.6 GB / 11887 个文件，未清理
7. **多账号切换的 GUI 路径**——CLI 测过，GUI 切换没跑过
8. **并发度上限的预算准入拒绝路径**——实测只到 3 路并发，
   准入拒绝分支没被触发过（那是最容易出并发 bug 的地方）

> ✅ 第 9 项已完成：本轮用新纪律**反过来审计了前四轮**，
> 并**补上了迭代 1 缺失的 L5 证据**（真机多代理并行：
> 162s 串行 → 36.1s 墙钟，4.5x）。详见
> `docs/verification/iter1-agent-team.json`。

---

## 七、给后续迭代的规矩

1. **新功能落地时，同时产出结构化验证报告**（放 `docs/verification/`），
   声明 level、claims、not_run、evidence，交给校验器过。
2. **端到端断言前，先确认前置条件成立**。
   迭代 4 的教训：连线数为 0 时，「隐藏后连线全部消失」会空跑通过。
   → 断言要写成 `assert before > 0`，别让「本来就没东西可测」蒙混过关。
3. **反向验证**：每条关键纪律都要有「破坏它 → 测试变红」的证明。
   光看测试通过不算证明测试有效。
4. **改了后端 ≠ 服务在跑新代码**。启动前核对
   `进程 CreationDate > 文件 LastWriteTime`，否则白测半天。
