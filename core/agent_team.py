# -*- coding: utf-8 -*-
"""墨溯多代理编排层 —— 把 Lancer 的编排外壳移植到墨溯的数据内核上。

对标来源：Lancer `apps/desktop/electron/orchestration/`
（raylee-ai/lancer，MIT）。研读结论见 `docs/benchmark-lancer.md`。

抄了什么（且为什么）：
  1. **分支树**：父会话可派生子分支，孙分支由子分支自己再派。
     Lancer 用 `OrchestrationChildThread` 记录父子关系，
     墨溯用 `Branch` 记录同样的关系，但执行体是**独立 ReAct 循环**
     而不是独立 Pi 会话（墨溯没有 Pi）。
  2. **回执状态机**：子分支跑完先产生 `reported` 状态，
     它的返回**确实回喂给直接父**之后才转 `accepted`。
     这是 Lancer 防止「半成品被当结论」的关键机制——不能省。
     Lancer 的实现在 `app-store.ts:3586`：投递确认后把证据状态
     从 reported 翻成 accepted；结算判据在 `branch-return-batch.ts:37`。
  3. **预算准入**：并发数 / 深度 / 节点数 / token 四维限额。
     Lancer 的 `TaskAdmission.reserve()` 在**第一个 await 之前**占位，
     避免并发超订——这个细节很关键，照抄。
  4. **子树结算**：孙分支必须先被直接父消费，才向上一级回传。
     否则会出现「日志瀑布」：每层都把孙层的原始日志原样转发。

Lancer 还有一个**平行**机制，墨溯也抄了，但要分清两者不是一回事：
  - Lancer `explicitAcceptanceEvidenceFromParentMessage`
    （`app-store-orchestration.ts:1929`）：父会话文本里写
    `orchestrator-accepted: <childId> <理由>` → 产生一条
    `orchestrator_acceptance` 证据（status=accepted）。
    **它不是结算门槛**，只是「父级明确说了采纳」的额外证据。
    墨溯对应 `mosu-accepted:` 标记 + `consume_branch()`。
    关键：**结算门槛永远不能依赖模型自觉写标记**——
    Lancer 全仓没有任何提示词教模型写这个字符串（只有 3 处代码引用），
    照抄成门槛 = 结算永远为假 = 白实现。

没抄什么（有意不做）：
  - Lancer 的分支是独立 Pi 会话 + Git worktree 隔离；
    墨溯是只读分析，同一份数据并发读没有隔离需求，
    用全局 DS_LOCK 串行化 SQLite 即可。
  - Lancer 的光线/流动边框动画：属于 UI 呈现层，
    墨溯前端是无构建步骤的原生 JS，收益比成本低（见路线图 P1-3）。

铁律（继承墨溯原有约束）：
  - **数字只能来自工具返回**。子代理的工具返回同样进数字审计范围。
  - **一条消息都不能编**。子代理的证据带出处，回传给父级时可核。
  - **纯只读**。子代理拿到的工具集与父级同源，全部只读。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from core.agent_tools import ToolBox
from core.locks import DS_LOCK

# ------------------------------------------------------------------ 预算

# 树级限额。默认值参考 Lancer 的 task-limits.ts（8/4/32/500000），
# 但墨溯要更保守：子代理的每次工具调用都要读 SQLite，
# 而 LLM 推理才是真正能并行的部分。
DEFAULT_BUDGET = {
    "max_concurrent": 3,   # 同时跑几个子代理
    "max_depth": 2,        # 主(0) → 子(1) → 孙(2)，孙不能再派
    "max_branches": 6,     # 整棵树最多几个分支
    "max_steps": 24,       # 整棵树最多几次工具调用（防无限查）
}


class BranchBudget:
    """树级预算准入。

    为什么要单独一个类：多代理最常见的翻车方式不是「查错」，
    而是「查太多」——每个子代理都觉得该多查两轮，
    树一深，总工具调用数呈平方级膨胀，用户等十分钟看不到答案。

    Lancer 的做法是 `TaskAdmission.reserve()`：
    在第一个 await 之前占位，防止两个并发调用同时通过检查后超订。
    这里照抄——**占位必须发生在起线程之前**。
    """

    def __init__(self, limits: dict | None = None):
        cfg = dict(DEFAULT_BUDGET)
        cfg.update(limits or {})
        self.max_concurrent = int(cfg["max_concurrent"])
        self.max_depth = int(cfg["max_depth"])
        self.max_branches = int(cfg["max_branches"])
        self.max_steps = int(cfg["max_steps"])
        self._lock = threading.Lock()
        self._reserved = 0
        self._steps_used = 0

    def reserve(self, n: int = 1) -> str | None:
        """占 n 个分支名额。返回 None 表示成功，否则返回拒绝原因。

        调用方**必须在第一个 await 之前**调用它。
        """
        with self._lock:
            if self._reserved + n > self.max_branches:
                return (f"分支数超限（已占 {self._reserved}，上限 {self.max_branches}）")
            self._reserved += n
            return None

    def release(self, n: int = 1) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - n)

    def try_step(self) -> bool:
        """申请一次工具调用名额。整棵树共享这个池子。"""
        with self._lock:
            if self._steps_used >= self.max_steps:
                return False
            self._steps_used += 1
            return True

    @property
    def steps_used(self) -> int:
        with self._lock:
            return self._steps_used


# ------------------------------------------------------------------ 分支

# 分支状态（对应 Lancer 的 OrchestrationChildThreadStatus）
BRANCH_QUEUED = "queued"
BRANCH_RUNNING = "running"
BRANCH_COMPLETE = "complete"
BRANCH_FAILED = "failed"

# 回执状态（对应 Lancer 的 evidence.status）
#
# 关键：Lancer 的 `accepted` 是**投递确认**，不是「模型说采纳了」。
#   - branch-return.ts:29  子分支跑完 → 证据 status="reported"
#   - app-store.ts:3586    报告确实送达父会话 → status 翻成 "accepted"
#   - branch-return-batch.ts:37  结算判据：没有 pending(reported) 的回执
# 也就是说 Lancer 的结算门槛是**程序判定的**，不依赖模型自觉。
RECEIPT_REPORTED = "reported"   # 分支跑完了，但返回还没回喂给直接父
RECEIPT_ACCEPTED = "accepted"   # 返回已回喂给直接父（Lancer 的投递确认）

# 显式接受标记（对应 Lancer 的 `orchestrator-accepted:`）
#
# 这是**平行的第二层语义**：父级明确表态「我采纳这条」。
# 它**不是**结算门槛（Lancer 全仓没有任何提示词教模型写这个串），
# 但它比「收到了」更强——能区分「父级看了并采信」和「父级只是收到了」。
# 墨溯用 `mosu-accepted:` 前缀，程序化正则提取，不靠模型自报字段。
ACCEPT_MARKER = "mosu-accepted:"
_ACCEPT_RE = re.compile(r"^\s*mosu-accepted:\s*(.+)$", re.IGNORECASE)

# 泳道调色板（对应 Lancer 的 ProcessLane.color）
#
# 为什么是**固定列表**而不是随机色 / hash 色：
# Lancer 的纪律是「颜色按泳道分配一次，全链路继承」。
# 随机色每次刷新都变，用户没法把「那条紫色的线」和某个子代理对上；
# hash 色在分支增删后会重新洗牌，同样对不上。
# 固定调色板按泳道索引取色 → 同一泳道永远是同一个颜色，
# 子分支继承父的泳道 → 整条链一个颜色，视觉上一眼看清归属。
#
# 颜色选的是**在明暗两套主题下都能看清**的一组（蓝/紫/青/琥珀/玫红/青绿）。
LANE_COLORS = [
    "#3b82f6",  # 蓝
    "#a855f7",  # 紫
    "#06b6d4",  # 青
    "#f59e0b",  # 琥珀
    "#ec4899",  # 玫红
    "#10b981",  # 青绿
    "#8b5cf6",  # 靛
    "#ef4444",  # 红
]

# 语义边配色（对应 Lancer 的 reference / retry 两种跨泳道虚线）
#
# 为什么不按泳道上色：一条语义边**横跨两个泳道**，
# 用其中一路的泳道色会让人误以为它是那条树线的延伸。
# 语义边必须**一眼看得出是另一种关系**——用中性色 + 虚线区分。
SEMANTIC_COLORS = {
    "shared-session": "#64748b",
    "shared-topic": "#94a3b8",
}

# 语义边的判据阈值
TOPIC_MIN_OVERLAP = 2      # 共同话题词至少几个才算「同一件事」
TOPIC_MAX_OVERLAP = 8      # 超过这个数说明只是通用词撞了，不算关联

# goal → 话题词集合的缓存（实时下发会反复算同一批分支）
_TOPIC_CACHE: dict[str, set[str]] = {}


def lane_color(lane: int) -> str:
    """泳道索引 → 颜色。越界时取模循环（不会崩，但也不会重复太多）。"""
    if lane is None or lane < 0:
        return LANE_COLORS[0]
    return LANE_COLORS[lane % len(LANE_COLORS)]


class Branch:
    """一个子分支的完整记录。

    对标 Lancer 的 `OrchestrationChildThread`，但去掉了 git 快照字段
    （墨溯分析的是聊天数据，不是代码，没有 commit 可锚）。
    保留的是**最关键的三样**：父子关系、状态、证据。
    """

    def __init__(self, goal: str, depth: int, parent: "Branch | None" = None,
                 lane: int | None = None):
        self.id = "b" + uuid.uuid4().hex[:8]
        self.goal = goal
        self.depth = depth
        self.parent_id = parent.id if parent else None
        # 泳道：顶层分支各自一条，子分支**继承父的**。
        # 这样「父 → 子」的连线天然同色，整条链一眼看清归属。
        self.lane = (parent.lane if parent is not None
                     else (lane if lane is not None else 0))
        self.status = BRANCH_QUEUED
        # 回执状态：reported（跑完未送达父级）→ accepted（已回喂给直接父）
        self.receipt = RECEIPT_REPORTED
        # 结算用（迭代 11）：本分支的返回被**直接父级**消费的时刻。
        # Lancer 的 settled() 语义是「子树只有在直接父消费了后代返回之后才结算」，
        # 所以光看 status=complete 不够——跑完了但父级没看，还不算结算。
        self.consumed_by: str = ""     # 消费方的分支 id（顶层则是主代理）
        self.consumed_at: float = 0.0
        # 显式接受（迭代 11）：父级在结论里写 `mosu-accepted: <分支id>` 时的记录。
        # 这跟 consumed_by 是**两件不同的事**：
        #   consumed_by = 程序判定的「送达」（结算门槛）
        #   accepted_by = 父级明确表态「我采信这条」（语义层，锦上添花）
        # 分开是因为把两者混成一个会让「送达即采信」的假象糊过去。
        self.accepted_by: str = ""
        self.accepted_at: float = 0.0
        self.accept_reason: str = ""
        self.report = ""            # 子代理的结论
        self.evidence: list[dict] = []
        self.steps: list[dict] = []
        self.numbers: set[str] = set()   # 子代理工具返回里出现过的数字
        self.tokens = 0
        self.cost = 0.0
        self.error = ""
        self.t0 = time.time()
        self.children: list["Branch"] = []
        if parent is not None:
            parent.children.append(self)

    @property
    def lane_color(self) -> str:
        """本分支的泳道颜色。同一泳道恒定，全链路继承。"""
        return lane_color(self.lane)

    @property
    def settled(self) -> bool:
        """本分支的子树是否已结算（见模块级 branch_settled）。"""
        return branch_settled(self)

    @property
    def accepted(self) -> bool:
        """父级是否**显式表态**采信了本分支的结论。

        跟 `receipt`（送达）分开：送达是程序判定的必然，显式接受是父级主动说的。
        """
        return bool(self.accepted_by)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "depth": self.depth,
            "parentId": self.parent_id,
            # 泳道与颜色：前端画树时按 lane 分列、按 laneColor 上色。
            # 颜色在这里算好下发，前端不自己配色——避免两边算法不一致。
            "lane": self.lane,
            "laneColor": lane_color(self.lane),
            "status": self.status,
            "receipt": self.receipt,
            # 结算信息（迭代 11）：前端要能看出「这条分支的返回被谁消费了」
            "consumedBy": self.consumed_by,
            "consumedAt": round(self.consumed_at, 3),
            "settled": self.settled,
            # 显式接受（迭代 11）：父级主动说「我采信这条」
            "accepted": self.accepted,
            "acceptedBy": self.accepted_by,
            "acceptedAt": round(self.accepted_at, 3),
            "acceptReason": self.accept_reason,
            "report": self.report,
            "evidence": self.evidence,
            "steps": self.steps,
            "tokens": self.tokens,
            "cost": round(self.cost, 2),
            "error": self.error,
            "children": [c.to_dict() for c in self.children],
        }


# ------------------------------------------------------------------ 团队

class AgentTeam:
    """一棵树的主持者。

    用法：
        team = AgentTeam(ds, llm=..., budget=BranchBudget())
        result = team.run_root(question)      # 主代理跑 ReAct
        # 主代理中途调 delegate_tasks → team 并行跑子代理 → 回传有界摘要

    线程模型：
      - 主代理在调用方线程跑
      - 子代理在 ThreadPoolExecutor 里跑，**LLM 推理并行**
      - 工具调用经 DS_LOCK 串行（SQLite 连接不能并发用）
    """

    def __init__(self, ds, llm=None, budget: BranchBudget | None = None,
                 max_steps: int = 8, tool_timeout: int = 45,
                 llm_factory=None):
        self.ds = ds
        self._llm = llm
        # 子代理的 LLM 客户端工厂。默认每子代理新建一个（连接不能共用）。
        # 测试注入假客户端——否则离线测试会真的打到网关，又慢又不确定。
        self.llm_factory = llm_factory
        self.budget = budget or BranchBudget()
        self.max_steps = max_steps
        self.tool_timeout = tool_timeout
        self.branches: list[Branch] = []
        self._lock = threading.Lock()

    @property
    def llm(self):
        if self._llm is None:
            from core.bot.llm import LlmClient
            self._llm = LlmClient(timeout=180)
        return self._llm

    def _new_llm(self):
        """给子代理一个**独立的** LLM 客户端。

        为什么不能共用：LlmClient 内部有持久 HTTP 连接（`self._conn`），
        多个线程共用一个连接会交错发送请求，直接串包。
        """
        if self.llm_factory is not None:
            return self.llm_factory()
        from core.bot.llm import LlmClient
        return LlmClient(timeout=180)

    # ------------------------------------------------------------ 预算守卫
    def can_delegate(self, depth: int) -> tuple[bool, str]:
        """能否在给定深度继续派分支。"""
        if depth >= self.budget.max_depth:
            return False, f"已达到 {self.budget.max_depth} 层深度上限，不再派子代理"
        with self._lock:
            if len(self.branches) >= self.budget.max_branches:
                return False, f"分支数已达上限 {self.budget.max_branches}"
        return True, ""

    # ------------------------------------------------------------ 跑一个分支
    def run_branch(self, goal: str, depth: int, parent: Branch | None = None,
                   on_step=None, lane: int | None = None) -> Branch:
        """跑一个子代理（独立 ReAct 循环），返回分支记录。

        与主代理的区别只有两点：
          1. 工具集受限（不给 delegate_tasks 之外的东西其实一样，
             但**深度上限**由 can_delegate 控制）
          2. 有界输出：结论截断到 MAX_REPORT_CHARS，
             防止子代理写一屏长文把父代理的上下文冲爆

        lane：泳道号。顶层传入（各自一条），子层不传（继承父的）。
        """
        br = Branch(goal, depth, parent, lane=lane)
        with self._lock:
            self.branches.append(br)
        br.status = BRANCH_RUNNING

        from core.agent_engine import SUBAGENT_SYSTEM, FINAL_HINT, DELEGATE_SCHEMA

        try:
            llm = self._new_llm()
            box = ToolBox(self.ds)
            messages = [
                {"role": "system", "content": SUBAGENT_SYSTEM},
                {"role": "user", "content": goal},
            ]
            tools = ToolBox.schemas()
            # 子代理**可以再派子代理**（Lancer 的递归天然成立：
            # 子分支调的是同一个 create_child_thread 工具）。
            # 但只在深度没到上限时给——孙层再派就该被拒了。
            # 之前这里没给，等于孙分支永远派不出来，
            # 「分支树」实际只有一层，「泳道继承」也无从验证。
            can_sub_delegate, _why = self.can_delegate(depth)
            if can_sub_delegate:
                tools = tools + [DELEGATE_SCHEMA]
            answer = ""
            steps: list[dict] = []

            for _ in range(self.max_steps):
                # 树级步骤池子耗尽 → 收尾
                if not self.budget.try_step():
                    answer = "（本次分析的查询次数已达上限，就当前已查到的内容作答）"
                    break
                resp = llm.chat_tools(messages, tools, temperature=0.2,
                                       max_tokens=1200)
                br.tokens += int((resp.get("usage") or {}).get("total_tokens") or 0)
                calls = resp.get("tool_calls") or []
                content = resp.get("content") or ""
                if not calls:
                    answer = content.strip()
                    break
                messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"],
                                      "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                        for c in calls if c.get("id") and c.get("name")
                    ],
                })
                for call in calls:
                    name = call.get("name") or ""
                    args = call.get("args") or {}
                    # ---- 子代理的二次委派：派孙分支
                    if name == "delegate_tasks":
                        payload, kids = self._sub_delegate(br, args, on_step)
                        # 注意：Branch.__init__ 里已经 `parent.children.append(self)`，
                        # 所以孙分支**本来就在** br.children 里。
                        # 这里再 extend 一次 → 同一个孙分支在树里出现两次
                        # → 前端把孙节点画了两遍。
                        # （迭代 12 端到端真跑才抓到：孙分支 id 出现在
                        #  语义边的两端列表里。）
                        # 所以这里只补**还没挂上**的，幂等。
                        known = {c.id for c in br.children}
                        br.children.extend([k for k in kids if k.id not in known])
                        run_steps = []
                        for kid in kids:
                            br.numbers |= kid.numbers
                            for ev in kid.evidence:
                                br.evidence.append(ev)
                        step = {
                            "index": len(steps) + 1,
                            "tool": "delegate_tasks",
                            "label": _tool_label("delegate_tasks", args),
                            "summary": (f"又派了 {len(kids)} 个子代理："
                                        + "、".join(k.goal[:20] for k in kids)),
                            "ok": True,
                            "branchId": br.id,
                        }
                        steps.append(step)
                        br.steps.append(step)
                        if on_step:
                            try:
                                on_step(step)
                            except Exception:
                                pass
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": payload,
                        })
                        # **送达点在 payload 回喂之后**（Lancer 同款）：
                        # app-store.ts:3586 是在分支报告**确实送达父会话**后
                        # 才把回执从 reported 翻成 accepted。
                        # 消费方是 br（直接父），不是主代理——
                        # 混成 "main" 就等于跳过了「直接父消费」这一环。
                        for kid in kids:
                            consume_branch(kid, br.id)
                        continue
                    res = box.run(name, args)
                    # 子代理的数字也要进审计范围——否则父代理用它给的数字
                    # 会被数字审计误判成「没有出处」
                    br.numbers |= _numbers_in(json.dumps(res.get("data") or {},
                                                         ensure_ascii=False,
                                                         default=str))
                    step = {
                        "index": len(steps) + 1,
                        "tool": name,
                        "label": _tool_label(name, res.get("args") or args),
                        "summary": res.get("summary") or "",
                        "cost": res.get("cost"),
                        "ok": not str(res.get("summary", "")).startswith("工具"),
                        "branchId": br.id,
                    }
                    steps.append(step)
                    br.steps.append(step)
                    if on_step:
                        try:
                            on_step(step)
                        except Exception:
                            pass
                    for ev in res.get("evidence") or []:
                        br.evidence.append(ev)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": _tool_payload_safe(res),
                    })
            else:
                messages.append({"role": "user", "content": FINAL_HINT})
                resp = llm.chat_tools(messages, [], temperature=0.2, max_tokens=1200)
                answer = (resp.get("content") or "").strip()

            br.report = (answer or "（子代理没有给出可显示的内容）")[:MAX_REPORT_CHARS]
            br.status = BRANCH_COMPLETE
            # 子代理对自己派出的孙分支也可以**显式表态接受**
            # （对标 Lancer 的 orchestrator-accepted）。
            # 提取是程序化的（正则扫文本），不靠模型自报字段。
            if br.children:
                try:
                    apply_accept_marks(answer or "", br.children, br.id)
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001 —— 子代理失败不能让整棵树崩
            br.status = BRANCH_FAILED
            br.error = f"{type(e).__name__}: {e}"
            br.report = f"（子代理执行失败：{br.error}）"

        br.cost = round(time.time() - br.t0, 2)
        return br

    # ------------------------------------------------------------ 并行派发
    def _sub_delegate(self, parent_branch: Branch, args: dict,
                      on_step=None) -> tuple[str, list]:
        """子代理发起二次委派（派孙分支）。

        返回 (给子代理看的 payload, 孙分支列表)。

        与主代理的委派有两个关键差别：
          1. **深度受限**：can_delegate 检查 depth，孙层不能再往下派
          2. **泳道继承**：孙分支不传 lane → 继承父的泳道号 →
             整条链一个颜色，父子关系在视觉上一目了然

        失败不崩：委派失败就回一句说明，让子代理自己决定怎么办。
        """
        from core.agent_engine import DELEGATE_SCHEMA  # 延迟导入避免循环
        goals = args.get("tasks") or []
        if isinstance(goals, str):
            goals = [goals]
        goals = [str(g).strip() for g in goals if str(g).strip()]
        if not goals:
            return json.dumps({"分支数": 0, "说明": "没有给出子任务"},
                              ensure_ascii=False), []

        ok, why = self.can_delegate(parent_branch.depth)
        if not ok:
            return json.dumps({"分支数": 0, "说明": why},
                              ensure_ascii=False), []

        def _on_grandchild_step(step):
            # 孙代理的步骤也要推到前端，否则轨迹里中间少一截
            if on_step:
                try:
                    on_step(step)
                except Exception:
                    pass

        try:
            kids = self.delegate(goals, depth=parent_branch.depth,
                                 parent=parent_branch, on_step=_on_grandchild_step)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"分支数": 0,
                               "说明": f"派发失败：{type(e).__name__}: {e}"},
                              ensure_ascii=False), []

        payload = json.dumps(branch_payload(kids), ensure_ascii=False,
                             default=str)
        if len(payload) > 4000:
            payload = payload[:3900] + "…（子代理结果过长已截断）"
        return payload, kids

    def delegate(self, goals: list[str], depth: int, parent: Branch | None = None,
                 on_step=None) -> list[Branch]:
        """并行跑若干子代理。

        返回的分支列表**按输入顺序**（不是完成顺序）——
        父代理看到的报告顺序必须稳定，否则它会以为
        「第一个是最重要的」，实际上只是跑得快。
        """
        goals = [g.strip() for g in (goals or []) if g and g.strip()]
        if not goals:
            return []
        if depth >= self.budget.max_depth:
            return []
        if len(goals) > self.budget.max_concurrent:
            goals = goals[: self.budget.max_concurrent]

        # 关键：**在第一个 await 之前**占位，防止并发超订
        reason = self.budget.reserve(len(goals))
        if reason:
            return []

        branches: list[Branch] = []
        # 同一批派出的顶层分支各占一条泳道。
        # 泳道号从「已有分支数」往后排，且**只增不减**——
        # 这样第二批委派不会跟第一批抢同一条泳道，
        # 用户看到的颜色是稳定的一一对应关系。
        # 有 parent 时**不分配新泳道**：孙分支继承父的，整条链一个颜色。
        lane_base = len(self.branches)
        lane_of = (lambda i: None) if parent is not None else (lambda i: lane_base + i)
        try:
            with ThreadPoolExecutor(max_workers=len(goals),
                                    thread_name_prefix="mosu-sub") as pool:
                futs = [pool.submit(self.run_branch, g, depth + 1, parent,
                                    on_step, lane_of(i))
                        for i, g in enumerate(goals)]
                for f in futs:
                    try:
                        branches.append(f.result())
                    except Exception as e:  # noqa: BLE001
                        br = Branch(f"（派发失败）{goals[len(branches)]}", depth + 1,
                                    parent, lane=lane_of(len(branches)))
                        br.status = BRANCH_FAILED
                        br.error = f"{type(e).__name__}: {e}"
                        br.report = f"（派发失败：{br.error}）"
                        branches.append(br)
        finally:
            self.budget.release(len(goals))
        return branches

    # ------------------------------------------------------------ 汇总
    def tree(self) -> list[dict]:
        """返回分支树（供前端展示）。"""
        with self._lock:
            return [b.to_dict() for b in self.branches if b.parent_id is None]


# ------------------------------------------------------------------ 有界回传

# 子代理结论回传给父代理时的长度上限。
# Lancer 用 400 字；墨溯稍宽（600），因为中文结论信息密度低，
# 但**必须有上限**——不然 6 个子代理各写 2000 字，父代理上下文直接爆。
MAX_REPORT_CHARS = 600


def branch_payload(branches: list[Branch]) -> dict:
    """把子分支结果压成父代理能吃的结构。

    刻意**只给结论和出处，不给原始工具返回**：
    父代理要的是「子代理查到了什么」，
    不是「子代理的每一条 SQL 结果」——后者会让上下文爆炸。
    """
    rows = []
    for br in branches:
        rows.append({
            "分支": br.id,
            "子任务": br.goal,
            "状态": br.status,
            "结论": br.report[:MAX_REPORT_CHARS],
            "依据": [
                {
                    "时间": ev.get("time"),
                    "谁": ev.get("sender"),
                    "内容": (ev.get("text") or "")[:120],
                }
                for ev in (br.evidence or [])[:3]
            ],
            "查询次数": len(br.steps),
            "耗时秒": round(br.cost, 1),
        })
    return {
        "分支数": len(rows),
        "分支结果": rows,
        "提示": ("这些是子代理独立查出来的结论。你要**核对**它们："
                 "数字要能在依据里找到出处，结论要和你的问题对得上。"
                 "不要原样照抄子代理的话——它可能查偏了。"
                 "用不到的分支就别写进答案。"),
    }


def branch_settled(br: Branch) -> bool:
    """单个分支的子树是否已结算。

    对标 Lancer 的 `settled()`（`branch-return-batch.ts:29-39`）：
      "A subtree settles only after its immediate parent consumed the descendant returns."

    Lancer 的真实判据是**三层**，不是一个布尔：
      ① 自身终态（complete / failed）
      ② 每个直接子分支自己也 settled（递归）
      ③ 每个直接子分支**没有 pending 的回执**——即它的返回已经被
         直接父消费掉了（Lancer 的 pending = evidence 里 status 仍是 "reported"
         的 branch-return 记录；投递给父会话后翻成 "accepted"）

    墨溯一一对应：
      ① br.status in (BRANCH_COMPLETE, BRANCH_FAILED)
      ② all(branch_settled(c) for c in br.children)
      ③ all(c.receipt == RECEIPT_ACCEPTED for c in br.children)

    为什么 ③ 不能省：分支跑完了但父级没看它的返回，
    那份返回就还没被纳入上层结论——此时说「已结算」是假的。
    Lancer 特意把「跑完」和「被消费」分成两件事，就是防这个。

    注意：**结算门槛是程序判定的**（送达即算），
    不依赖模型自觉写什么标记——这是照抄 Lancer 的关键。
    「父级显式表态采信」是另一层（accepted_by），不参与结算。
    """
    if br.status not in (BRANCH_COMPLETE, BRANCH_FAILED):
        return False
    if not all(branch_settled(c) for c in br.children):
        return False
    return all(c.receipt == RECEIPT_ACCEPTED for c in br.children)


def settle_children(branches: list[Branch]) -> bool:
    """子树结算：所有分支都跑完 **且** 它们的返回都被直接父消费过。

    迭代 11 之前这个函数只是「所有子分支都终态」，
    注释还写着「子代理本身不派孙分支」——**那是错的**：
    `_sub_delegate` 真的能派孙分支（孙层拿得到 delegate_tasks 工具）。
    注释与实现脱节，导致这个函数定义完就没人调用——
    结算语义从来没真正落地过。
    """
    return all(branch_settled(b) for b in branches)


def consume_branch(br: Branch, by: str) -> None:
    """标记「某分支的返回已被消费方收到」——**送达**，不是「采信」。

    `by` 是消费方的标识：子分支的 id，或主代理（传 "main"）。

    这是 Lancer 的**回执状态机**（`reported → accepted`）：
    `app-store.ts:3586` 在分支报告确实送达父会话后，
    把该报告的 evidence status 从 "reported" 翻成 "accepted"；
    `branch-return-batch.ts:37` 据此判定子树是否结算。

    墨溯之前定义了 `RECEIPT_ACCEPTED` 常量却从没赋值过——
    意味着「父级核对」这个环节在代码里根本不存在。

    幂等：重复送达不重复改写首次消费方。
    """
    if br.receipt == RECEIPT_ACCEPTED:
        return          # 已送达过，不重复改写（幂等）
    br.receipt = RECEIPT_ACCEPTED
    br.consumed_by = by
    br.consumed_at = time.time()


def consume_tree(branches: list[Branch], by: str = "main") -> None:
    """把一批分支标记为「已被消费方收到」。

    同时把它们的**子分支**也标记为被**它们自己**消费——
    因为子分支的返回已经通过父分支的结论传递上去了
    （父分支的 payload 里带着孙分支的结果）。
    """
    for br in branches:
        consume_branch(br, by)
        for c in br.children:
            consume_branch(c, br.id)


def parse_accept_marks(text: str) -> list[tuple[str, str]]:
    """从一段文本里提取「显式接受」标记。

    对标 Lancer 的 `explicitAcceptanceEvidenceFromParentMessage`
    （`app-store-orchestration.ts:1929`）——它用正则
    `/^orchestrator-accepted:\\s*(\\S+)\\s*(.*)$/i` 逐行扫描父会话文本，
    命中就产出一条 `orchestrator_acceptance` 证据。

    墨溯用 `mosu-accepted:` 前缀（跟 Lancer 的 `orchestrator-accepted:` 同思路），
    同样**程序化提取，不靠模型自报字段**——这是迭代 10
    「分级由程序判，不由模型说」的延续。

    返回 [(分支id, 理由), ...]，按出现顺序。
    """
    out: list[tuple[str, str]] = []
    for line in (text or "").split("\n"):
        m = _ACCEPT_RE.match(line)
        if not m:
            continue
        rest = m.group(1).strip()
        if not rest:
            continue
        # 第一段是分支 id，其余是理由（可选）
        parts = rest.split(None, 1)
        bid = parts[0].strip()
        reason = parts[1].strip() if len(parts) > 1 else ""
        out.append((bid, reason))
    return out


def apply_accept_marks(text: str, branches: list[Branch],
                       by: str = "") -> list[Branch]:
    """把文本里的接受标记落到对应分支上。

    `by` 是表态方（顶层是主代理，子层是父分支 id）。
    返回被显式接受的分支列表。

    幂等：同一条分支被接受多次，只记第一次。
    """
    marks = parse_accept_marks(text)
    if not marks:
        return []
    by_id = {b.id: b for b in branches}
    hit: list[Branch] = []
    for bid, reason in marks:
        br = by_id.get(bid)
        if br is None:
            continue          # 标记指向不存在的分支 → 忽略（模型可能编了个 id）
        if br.accepted_by:
            continue          # 已表态过，不覆盖（幂等）
        br.accepted_by = by or "main"
        br.accepted_at = time.time()
        br.accept_reason = reason
        hit.append(br)
    return hit


# ------------------------------------------------------------------ 工具实现

# 从 agent_engine 借工具，延迟导入避免循环依赖
def _tool_label(name: str, args: dict) -> str:
    from core.agent_engine import _tool_label as f
    return f(name, args)


def _tool_payload_safe(res: dict) -> str:
    from core.agent_engine import _tool_payload_safe as f
    return f(res)


def _numbers_in(text: str) -> set[str]:
    from core.agent_engine import _numbers_in as f
    return f(text)


# ------------------------------------------------------------------ 语义关联
#
# 迭代 12：对标 Lancer 的**跨泳道语义边**。
#
# Lancer 的 process-curve-routes.ts 里，树上有两类线：
#   ① **树线**（父子关系）——实线
#   ② **语义边**（reference / retry）——虚线，横跨泳道
# 墨溯迭代 4 只抄了 ①（父子树线 + 泳道分列），迭代 12 补 ②。
#
# Lancer 的语义边来自**事件流里的显式引用**
# （event.reference / event.retryTarget），
# 墨溯没有等价的东西——子代理之间不会互相引用。
# 所以这里**不抄判据，抄纪律**：
#   - 语义边是**虚线**，跟树线视觉上分开
#   - 端点绑定在**语义标识**（data-anchor-key）上，不是坐标
#   - **锚点读不到就整条丢弃**，绝不画到邻近节点
#
# 判据用墨溯自己的数据（都是程序可算的，不靠模型自报）：
#   shared-session —— 两个分支的**quote 类证据**里出现了同一个 sessionId
#                    （都真读到了同一个人的原话，是最强的关联）
#   shared-topic   —— 两个分支的目标文本里有共同的话题词
#                    （同一件事的不同角度）
#
# 为什么只有两种（曾经的第三种被砍掉了）：
#   「时间区间重叠」在并行委派下**必然成立**——
#   三个子代理一起跑，两两都重叠，那条线等于每对都连，
#   变成纯噪音。而噪音会稀释真正有信息的线，
#   用户看多了就不信这层标记了。**假关系比没线更糟。**
#
# 为什么 shared-session 只看 quote 类证据：
#   `find_contact` 会为**搜到的每个会话**产一条 stat 证据，
#   那只说明「搜到了这个人」，不是「读了这个人的消息」。
#   把 stat 也算进去 → 「查了张」和任何分支都连上 → 满屏假线。
#   quote 证据才是**真的读到了原话**。
#
# 为什么宁可少画也不多画：
#   用户看到一条语义边，会以为「这两个分支真的有关系」。
#   画错的代价远大于漏画——跟迭代 4 的「假关系比没线更糟」同一条纪律。


def _branch_quoted_sessions(br: Branch) -> set[str]:
    """这个分支**真正读到过原话**的会话 ID 集合。

    只取 kind == "quote" 的证据——
    stat 证据只说明「查到了这个会话」，不代表读过它。
    """
    out: set[str] = set()
    for ev in (br.evidence or []):
        if ev.get("kind") != "quote":
            continue
        sid = ev.get("sessionId")
        if sid:
            out.add(str(sid))
    return out


def _branch_quoted_senders(br: Branch) -> set[str]:
    """这个分支读过原话的**说话人**（人类可读名）。

    用来给语义边写人话标签——
    sessionId 是 wxid（wxid_xxx / 群 username），直接显示用户看不懂。
    """
    out: set[str] = set()
    for ev in (br.evidence or []):
        if ev.get("kind") != "quote":
            continue
        who = (ev.get("sender") or "").strip()
        if who and who != "我":
            out.add(who)
    return out


def _branch_topics(br: Branch) -> set[str]:
    """这个分支目标文本里的话题词。

    只用**目标文本**（goal），不用结论（report）——
    结论里全是数字和专有名词，容易撞词。
    """
    goal = br.goal or ""
    if not goal.strip():
        return set()
    # 按 goal 文本缓存：实时下发时每次重算会反复跑 jieba（慢），
    # 而同一批分支的 goal 在一轮里是不变的。
    cached = _TOPIC_CACHE.get(goal)
    if cached is not None:
        return cached
    try:
        from core.analytics import extract_keywords
        kws = extract_keywords([goal], top=12)
        out = {w for w, _ in kws}
    except Exception:
        out = set()
    if len(_TOPIC_CACHE) < 512:
        _TOPIC_CACHE[goal] = out
    return out


def _all_branches(branches: list[Branch]) -> list[Branch]:
    """摊平整棵分支树（含孙分支），按 id 去重。

    AgentRun.branches 只装**顶层**分支，孙分支嵌在 Branch.children 里。
    直接对 run.branches 两两比较 → 只会看到顶层之间，
    隔代的关联全漏。所以这里先摊平。

    为什么去重：构造 Branch(parent=P) 时会自动 `P.children.append(self)`。
    如果调用方既把 child 放进顶层列表、它又在 parent.children 里，
    摊平后同一个分支会出现两次 → 会出现「自己连自己」的自环。
    """
    out: list[Branch] = []
    seen: set[str] = set()

    def _walk(items):
        for b in items or []:
            bid = getattr(b, "id", None)
            if not bid or bid in seen:
                continue
            seen.add(bid)
            out.append(b)
            _walk(getattr(b, "children", []) or [])
    _walk(branches)
    return out


def semantic_links(branches: list[Branch]) -> list[dict]:
    """算分支之间的语义关联（跨泳道虚线的数据源）。

    对标 Lancer `processTreeRoutes` 的 reference / retry 边，
    但**判据是墨溯自己的**（见本节开头的说明）。

    返回的每条：
        {"from": 分支id, "to": 分支id, "kind": 判据名,
         "label": 人话说明, "color": 配色}

    刻意**只给最强的一种关联**：一对分支只画一条线。
    同时给「同一个会话」+「同一个话题」两条，用户会以为有两件事，
    其实是一件事的两种说法——噪音。

    只在**非父子**分支之间算：父子之间已经有树线了，
    再加一条语义线是重复。
    """
    out: list[dict] = []
    flat = _all_branches(branches)
    if len(flat) < 2:
        return out

    sessions = {b.id: _branch_quoted_sessions(b) for b in flat}
    senders = {b.id: _branch_quoted_senders(b) for b in flat}
    topics = {b.id: _branch_topics(b) for b in flat}

    for i, a in enumerate(flat):
        for b in flat[i + 1:]:
            # 同一个分支跟自己不连（自环没有任何语义）
            if a.id == b.id:
                continue
            # 父子之间不画（树线已经表达了）
            if a.parent_id == b.id or b.parent_id == a.id:
                continue

            kind = ""
            label = ""

            # 判据 1：读到过同一个会话的同一个人（最强）
            shared = sessions[a.id] & sessions[b.id]
            if shared:
                kind = "shared-session"
                # 标签用**人名**（sender），不用 sessionId——
                # sessionId 是 wxid/群 username，用户看不懂「都查过 wxid_xxx」。
                who = sorted(senders[a.id] & senders[b.id])
                label = ("都读过「%s」的聊天" % "、".join(who[:2]) if who
                         else "都查了同一个会话")

            # 判据 2：共同话题词
            if not kind:
                common = topics[a.id] & topics[b.id]
                if TOPIC_MIN_OVERLAP <= len(common) <= TOPIC_MAX_OVERLAP:
                    kind = "shared-topic"
                    label = "同一话题：" + "、".join(sorted(common)[:3])

            if not kind:
                continue

            out.append({
                "from": a.id,
                "to": b.id,
                "kind": kind,
                "label": label,
                "color": SEMANTIC_COLORS.get(kind, SEMANTIC_COLORS["shared-topic"]),
            })
    return out


def attach_semantic_links(branch_dicts: list[dict], links: list[dict]) -> None:
    """把语义关联挂到分支 dict 上（前端按分支 id 取）。

    挂成**每个分支一份**（两端都有），前端画线时不用反查。
    """
    by_id: dict[str, list[dict]] = {}
    for lk in links or []:
        by_id.setdefault(lk["from"], []).append(lk)
        by_id.setdefault(lk["to"], []).append(lk)

    def _walk(items):
        for it in items or []:
            it["semanticLinks"] = by_id.get(it.get("id"), [])
            _walk(it.get("children"))
    _walk(branch_dicts)
