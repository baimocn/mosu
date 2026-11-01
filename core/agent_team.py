# -*- coding: utf-8 -*-
"""墨溯多代理编排层 —— 把 Lancer 的编排外壳移植到墨溯的数据内核上。

对标来源：Lancer `apps/desktop/electron/orchestration/`
（raylee-ai/lancer，MIT）。研读结论见 `docs/benchmark-lancer.md`。

抄了什么（且为什么）：
  1. **分支树**：父会话可派生子分支，孙分支由子分支自己再派。
     Lancer 用 `OrchestrationChildThread` 记录父子关系，
     墨溯用 `Branch` 记录同样的关系，但执行体是**独立 ReAct 循环**
     而不是独立 Pi 会话（墨溯没有 Pi）。
  2. **回执状态机**：子分支跑完只产生 `reported` 回执，
     父级核对后才升级 `accepted`。这是 Lancer 防止「半成品被当结论」
     的关键机制——不能省。
  3. **预算准入**：并发数 / 深度 / 节点数 / token 四维限额。
     Lancer 的 `TaskAdmission.reserve()` 在**第一个 await 之前**占位，
     避免并发超订——这个细节很关键，照抄。
  4. **子树结算**：孙分支必须先被直接父消费，才向上一级回传。
     否则会出现「日志瀑布」：每层都把孙层的原始日志原样转发。

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
RECEIPT_REPORTED = "reported"   # 子分支跑完了，但父级还没核对
RECEIPT_ACCEPTED = "accepted"   # 父级核对通过，可以当结论用

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
        self.receipt = RECEIPT_REPORTED
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
                        br.children.extend(kids)
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


def settle_children(branches: list[Branch]) -> bool:
    """子树结算：所有子分支都处理完了才返回 True。

    对标 Lancer 的 `settled()`：
      "A subtree settles only after its immediate parent consumed the descendant returns."

    墨溯的简化版——子代理本身不派孙分支（深度上限 2 且孙层无派发工具），
    所以结算退化成「所有直接子分支都终态」。
    保留这个函数是为了语义正确：父代理在汇总前应确认子树已收敛。
    """
    return all(b.status in (BRANCH_COMPLETE, BRANCH_FAILED) for b in branches)


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
