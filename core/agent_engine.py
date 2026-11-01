# -*- coding: utf-8 -*-
"""墨溯 Agent 引擎 —— 让模型自己动手查数据，而不是只转述预塞的料。

问题（2026-11-03 用户反馈「ai 功能好差劲」）：
    旧洞察是固定管道：预先采集 7 条事实 + 8 条语料样本 → 模型一次调用 → 结束。
    模型被绑住了手——问「我和张三上个月聊了什么」，它压根看不到张三的记录。
    答案只能停在「数据里没有这一项」。

现在改成 Agentic（ReAct 循环）：
    模型 → 选工具 → 墨溯执行（只读）→ 结果回喂 → 模型继续 → 直到给出最终答案。

三条铁律（对齐竞品 CipherTalk 学来的经验，但按墨溯的约束重新设计）：
  1. **数字只能来自工具结果**。不给模型写 SQL 的能力（竞品给了 query_sql 兜底），
     杜绝「模型自己编一个数」。统计一律走 chat_stats。
  2. **每次工具调用都留出处**。evidence 里带 sessionId/localId/time/sender，
     模型据此标注引用，前端据此做「点依据跳原文」。
  3. **检索只给线索，不给定论**。模型要用消息原文下结论，
     必须先 read_timeline / search_messages 拿到原文——提示词里明写。

与竞品的差异（有意为之）：
  - 竞品 30+ 工具（记忆/语音/图片/插件/文件），墨溯只做**只读数据查询**
  - 竞品依赖 embedding 做语义检索，墨溯**零外部依赖**——
    用「关键词 + 精确时间范围 + 会话定向」三件套覆盖主要查询
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime

from core.agent_tools import ToolBox

# ------------------------------------------------------------------ 提示词

SYSTEM = """你在帮用户分析他/她自己的微信聊天记录。这是**你自己的真实数据**——
好友的聊天、群里的闲聊、一整年的对话都在里面。

# 你的能力
你有工具可以查这份数据。看到问题先想清楚「要回答这个，需要查什么」，
然后调工具去查，拿到结果再回答。**不要凭印象回答，也不要因为数据不在眼前就说查不到。**

# 工具怎么用
- resolve_time：把「上个月」「去年」「最近三个月」换算成绝对日期。**先换算再查**。
- chat_stats：问数量、排名、分布、时间跨度、类型构成、关键词频次时用它。
  **所有数字都从这里来**，别自己数、别自己算百分比。
- find_contact：把人名/群名换成 sessionId。任何「某人」相关的查询都先走它。
- list_sessions：不知道有哪些聊天对象时用它。
- search_messages：找含某个词的消息原文。
- read_timeline：读某个会话某段时间的连续消息——回答「某人某天聊了什么」用它。
- session_detail：某个人的关系画像（往来量、我方占比、活跃天数、共同话题）。
- my_profile：我本人的说话习惯与作息。
- search_moments：查朋友圈。
- delegate_tasks：把问题拆成几个互不依赖的子任务，派给子代理**并行**去查。
  **什么该用它**：问题里涉及**两个以上互不相干的对象**时
  （「分别分析张三和李四王五」「同时查这三个时间段」），
  你自己一个个查会占满步数还慢。**什么不该用**：只查一个人、
  或者几个子任务有先后依赖（那还是要你自己按顺序查）。
  派之前先自己确认这些人存在（find_contact），别派一堆查不到的任务。
  子代理回来的结论你要**核对**再用——它可能查偏了。

# 硬规矩
1. **数字必须来自工具返回的内容**。你自己算出来的百分比、总数、排名都不算数。
   算不了就调工具。工具没返回的数字，就说「这我没查到」。
2. **一条消息都不能编**。引用聊天内容必须来自 search_messages / read_timeline
   的返回，引用时带上时间和说话人。
3. **检索结果只是线索，不是结论**。要断定「他当时是什么意思」「他们是什么关系」，
   得用 read_timeline 把前后文读完再下结论，不能只看片段就定性。
4. **时间和范围要说清**。你查了哪段时间、哪个会话，必须在回答里交代。
5. 工具返回空或报错，如实说「没查到 / 查的时候出错了」，不要编一个像样的答案。
6. 别过度检索。问题简单就一两个工具查完；别为了凑数把无关数据也翻出来。
7. **不要"修正"工具给的数字**。如果你觉得工具返回的数和别处不一致，
   就把两处都原样写出来说明不一致，**不要自己挑一个"更准的"报上去**。
   （实测踩过：工具返回 2814 条，模型自己改口成 2904 条——
   用户按 2904 去核对，怎么都对不上。）

# 时间
现在是 {now}（{weekday}）。
**任何相对时间（「上个月」「去年」「最近三个月」「10 月」）都必须先调 resolve_time
换算成绝对日期，不要自己算。** 实测踩过：问「10 月份发了多少条」，
模型自己换算成 2025-10（现在是 2026-11，应指 2026-10），整条答案全错，
而用户看不出哪里错了。

没有年份的月份（如「10 月」）默认指**最近的那个** 10 月，也就是去年 10 月到今年
10 月之间最靠近今天的那个。不确定就先 resolve_time，它会按当前日期算。

# 怎么写
- 开头直接给结论，不要「根据数据来看」这种铺垫。
- 中间该展开就展开，讲清楚「为什么得出这个结论」。
- 每个结论后面标依据：用「（依据：<工具返回的原话或数字>）」。
  依据要**照抄工具返回里的内容**，不要自己改写数字。
- 口语化，像朋友帮你分析事情，不要写成报告腔、不要「综上所述」「建议如下」。
- 有反直觉的地方要指出来（比如「消息最多的人其实不是你主动找得最多的」）。
- 不确定就说不确定，别硬圆。
- 中文回答。"""

FINAL_HINT = """现在给出最终答案。

要求：
- 直接回答用户的问题，开头一句话给结论。
- 该标依据的地方标「（依据：…）」，依据照抄工具返回的内容。
- 说清你查了哪段时间、哪些会话、用的什么口径。
- 不要再调工具，直接写答案。"""


# 子代理专用提示词。父代理的提示词里有一大段「怎么跟用户说话」，
# 子代理不需要（它不面向用户），但需要额外的两条：
#   1. 只查这一个子任务，别顺手多查
#   2. 输出要短（父代理要汇总，塞不进一屏长文）
SUBAGENT_SYSTEM = """你是主助手派出的子助手，负责**一个**具体子任务。

# 你的子任务
（见用户消息）

# 规矩
1. **只查你这个子任务**。别顺手查别的、别扩大范围。
2. **数字必须来自工具返回的内容**，不要自己算、不要自己估。
3. **引用消息必须来自 search_messages / read_timeline 的返回**，
   引用时带上时间和说话人。
4. 检索结果只是线索，要下结论得用 read_timeline 把前后文读完。
5. 相对时间必须先调 resolve_time 换算成绝对日期。
6. 查不到就明说「查不到」，**不要编一个像样的答案**。

# 怎么写
- 开头一句话给结论，然后列 2–4 条要点。
- 每条要点后面标依据：「（依据：<工具返回的原话或数字>）」。
- 说清你查了哪段时间、哪个会话。
- **总共控制在 500 字以内**——你的结果会被汇总进主助手的回答，
  写太长会把主助手的上下文冲爆，等于白干。
- 中文。"""


# ------------------------------------------------------------------ 引擎

class AgentRun:
    """一次 Agent 运行的完整轨迹。

    前端要展示「它查了什么」——这是 Agentic 相对固定管道最大的体验差异：
    用户能看见模型在动脑子，而不是黑盒等 30 秒。
    """

    def __init__(self, question: str):
        self.question = question
        self.steps: list[dict] = []      # 工具调用轨迹
        self.evidence: list[dict] = []   # 去重后的出处
        self.branches: list = []         # 子分支记录（多代理编排）
        self.answer = ""
        self.tokens = 0
        self.t0 = time.time()
        # 工具返回里出现过的所有数字（字符串形式）——用来核对答案里的数字有没有出处
        self.tool_numbers: set[str] = set()
        # 阶段契约判定结果（core/pipeline.py）。
        # 没有它就只是一段文本答案；有了它，每个阶段的状态码可机械核验。
        self.contract: dict = {}

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "steps": self.steps,
            "evidence": self.evidence,
            "branches": [b.to_dict() for b in self.branches],
            "tokens": self.tokens,
            "cost": round(time.time() - self.t0, 2),
            # 阶段契约：stages / gates / forbidden / okToProceed
            "contract": self.contract,
        }


class InsightAgent:
    """Agent 引擎。一个实例复用一个 ToolBox（会话列表缓存）。"""

    # 单轮最多几次工具调用。竞品用 maxSteps=10，
    # 这里给 10 次：够覆盖「解析人名 → 查统计 → 读原文」这类三段式链路，
    # 又不至于让用户等太久。
    #
    # 从 8 提到 10 的原因（端到端实测）：
    # 问「分别分析三个人」时，模型串行查要 15 步（3 找人 + 3 画像 + 3 关键词
    # + 3 读原文 + 2 补充），8 步根本不够，它会被截断。
    # 有 delegate_tasks 时主代理只需 4-5 步，但模型**不一定**用委派
    # （比如它判断只有一个人值得深挖），所以上限还是要留够。
    MAX_STEPS = 10
    # 单次工具调用的超时上限（秒）
    TOOL_TIMEOUT = 45

    def __init__(self, ds, llm=None, max_steps: int | None = None,
                 team=None):
        self.ds = ds
        self.box = ToolBox(ds)
        self._llm = llm
        self.team = team          # AgentTeam：多代理编排（可为 None）
        if max_steps:
            self.MAX_STEPS = max_steps
        # 外部引导（steer）：跑的过程中塞进来的额外指令。
        # 用锁保护：派活通道的 steer 线程写、主循环线程读。
        self._steer_lock = threading.Lock()
        self._pending_steer: list[str] = []
        # 最近一次注入的引导原文（供落盘时记录「引导了什么」）
        self.last_steer: list[str] = []

    def steer(self, message: str) -> None:
        """插入一条引导，下一轮模型调用前会被送进上下文。

        对标 Lancer 的 steer 语义（control-service.ts:245）：
        不是「打断当前这轮」，而是「在当前这轮还在跑的时候，把新指令递进去」，
        让模型自己决定是调整方向还是照原计划走完。

        为什么用「下一轮注入」而不是立刻打断：
        打断会丢掉模型已经查到的东西（工具调用可能已经发出去了），
        而引导是**增量**的——模型手里已有的证据不丢，还多了条新约束。
        """
        msg = (message or "").strip()
        if not msg:
            return
        with self._steer_lock:
            self._pending_steer.append(msg)

    def _take_steer(self) -> list[str]:
        """取走并清空待注入的引导。"""
        with self._steer_lock:
            out = list(self._pending_steer)
            self._pending_steer.clear()
            return out

    def _inject_steer(self, messages: list[dict]) -> bool:
        """把待注入的引导变成 user 消息追加到上下文。返回是否注入了。

        同时把**引导的原文**记到 self.last_steer 里——落盘时要用，
        否则回看历史只看到「收到外部引导」这种固定文案，
        根本不知道当时引导了啥（这跟「摘要与详情同名不同型」是同一类问题：
        有信息但看不出来）。
        """
        st = self._take_steer()
        if not st:
            return False
        body = "；".join(st)
        self.last_steer = st
        messages.append({
            "role": "user",
            "content": (f"【来自外部的补充指令，请立即纳入考虑】{body}"),
        })
        return True

    @property
    def llm(self):
        if self._llm is None:
            from core.bot.llm import LlmClient
            self._llm = LlmClient(timeout=180)
        return self._llm

    # ------------------------------------------------------------ 主循环
    def run(self, question: str, history: list[dict] | None = None,
            on_step=None) -> AgentRun:
        """跑一次 Agent。

        history: 之前的对话轮次 [{"role": "user"/"assistant", "content": ...}]，
                 让用户能追问「那上个月呢」「他是谁」。
        on_step: 回调 (step_dict) -> None，用于流式把轨迹推给前端。
        """
        question = (question or "").strip()
        if not question:
            raise ValueError("问题不能为空")

        run = AgentRun(question)
        now = datetime.now()
        weekday = "星期" + "一二三四五六日"[now.weekday()]
        sys_prompt = SYSTEM.format(now=now.strftime("%Y-%m-%d %H:%M"), weekday=weekday)
        messages: list[dict] = [{"role": "system", "content": sys_prompt}]
        # 历史轮次（裁到最近 6 轮，防止上下文无界）
        for h in (history or [])[-12:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": question})

        tools = ToolBox.schemas()
        # 有编排团队时才给「派子代理」这个工具。
        # 没有 team 时模型压根不知道自己能派活，避免它喊一个不存在的工具。
        if self.team is not None:
            tools = tools + [DELEGATE_SCHEMA]
        for step_i in range(self.MAX_STEPS):
            # 外部引导（steer）：在每轮模型调用**之前**注入。
            # 放这里而不是循环外，是因为引导可能随时到达——
            # 循环外只注一次的话，运行中途来的引导就丢了。
            if self._inject_steer(messages):
                _steer_text = "；".join(self.last_steer)
                run.steps.append({
                    "index": len(run.steps) + 1,
                    "tool": "steer",
                    "label": "收到外部引导",
                    # 记录引导原文（截断），否则回看历史只知道「有过引导」，
                    # 不知道引导了啥
                    "summary": (_steer_text[:80] + "…")
                    if len(_steer_text) > 80 else _steer_text,
                    "ok": True,
                })
                if on_step:
                    try:
                        on_step(run.steps[-1])
                    except Exception:
                        pass
            resp = self.llm.chat_tools(messages, tools, temperature=0.3,
                                       max_tokens=1500)
            run.tokens += int((resp.get("usage") or {}).get("total_tokens") or 0)
            calls = resp.get("tool_calls") or []
            content = resp.get("content") or ""

            # ---- 模型直接给了答案（没调工具）→ 收尾
            if not calls:
                run.answer = content.strip()
                if not run.answer:
                    run.answer = "（模型没有给出可显示的内容）"
                break

            # ---- 有工具调用 → 执行并回喂
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

                # ---- 委派：并行跑子代理，把有界摘要回喂
                if name == "delegate_tasks" and self.team is not None:
                    payload, branches = self._run_delegate(args, on_step)
                    run.branches.extend(branches)
                    # 子代理工具返回里的数字也要进审计范围——
                    # 否则父代理引用子代理给的数字，会被误判成「没有出处」
                    for br in branches:
                        run.tool_numbers |= br.numbers
                    # 子代理的证据也要进总证据池（前端要能点开看原文）
                    for ev in _branch_evidence(branches):
                        key = (ev.get("sessionId"), ev.get("localId"))
                        if not any((e.get("sessionId"), e.get("localId")) == key
                                   for e in run.evidence):
                            run.evidence.append(ev)
                    # 委派本身也要记一步，前端轨迹里要看得见「派了子代理」
                    run.steps.append({
                        "index": len(run.steps) + 1,
                        "tool": "delegate_tasks",
                        "label": _tool_label("delegate_tasks", args),
                        "summary": (f"派了 {len(branches)} 个子代理并行查："
                                    + "、".join(b.goal[:20] for b in branches)),
                        "cost": round(sum(b.cost for b in branches), 2),
                        "ok": True,
                    })
                    if on_step:
                        try:
                            on_step(run.steps[-1])
                        except Exception:
                            pass
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": payload,
                    })
                    continue

                res = self.box.run(name, args)
                # 记下工具返回里的所有数字，末尾用来核对答案
                run.tool_numbers |= _numbers_in(json.dumps(res.get("data") or {},
                                                           ensure_ascii=False, default=str))
                step = {
                    "index": len(run.steps) + 1,
                    "tool": name,
                    # 用人名版的参数（ToolBox._humanize 已把 wxid 换成昵称），
                    # 否则轨迹里全是 wxid_xxx，用户看着头疼
                    "label": _tool_label(name, res.get("args") or args),
                    "summary": res.get("summary") or "",
                    "cost": res.get("cost"),
                    "ok": not str(res.get("summary", "")).startswith("工具"),
                }
                run.steps.append(step)
                if on_step:
                    try:
                        on_step(step)
                    except Exception:
                        pass
                # 收出处
                for ev in res.get("evidence") or []:
                    key = (ev.get("sessionId"), ev.get("localId"))
                    if not any((e.get("sessionId"), e.get("localId")) == key
                               for e in run.evidence):
                        run.evidence.append(ev)
                # 回喂给模型
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": _tool_payload_safe(res),
                })
        else:
            # 步数用尽 → 强制收尾
            messages.append({"role": "user", "content": FINAL_HINT})
            resp = self.llm.chat_tools(messages, [], temperature=0.3, max_tokens=1500)
            run.answer = (resp.get("content") or "").strip() or "（未能在步数限制内给出答案）"

        # ---- 阶段契约判定
        #
        # 顺序很要紧：**先判契约，再拼审计提示**。
        # 审计提示本身含数字（「2904」会被写进提醒里），
        # 先拼提示再判定的话，契约会拿「提示里的数字」当答案的数字来自证，
        # 判出「有出处」——自己给自己签了通过。
        #
        # 契约判定也**不改变**答案：它只产出一个可核验的结构（stages/gates），
        # 答案里该提醒什么仍由 _audit_numbers 决定。两件事分开做、各管各的。
        try:
            from core import pipeline
            run.contract = pipeline.evaluate({
                "steps": run.steps,
                "evidence": run.evidence,
                "answer": run.answer,
                "toolNumbers": run.tool_numbers,
                "branches": run.branches,
                "unsourcedNumbers": _unsourced_numbers(run),
            })
        except Exception:
            # 契约判定失败**不能连带整轮失败**——答案已经算出来了。
            # 契约是附加的验收信息，不是主链路。
            run.contract = {}

        run.answer = _audit_numbers(run)
        return run

    # ------------------------------------------------------------ 委派
    def _run_delegate(self, args: dict, on_step=None) -> tuple[str, list]:
        """执行一次委派：并行跑子代理，回传有界摘要。

        返回 (给模型看的 payload, 分支记录列表)。

        失败不能崩主流程——委派失败就当成一个空结果回喂，
        让模型自己决定是继续用自己的工具查，还是直接说做不到。
        """
        from core.agent_team import branch_payload
        goals = args.get("tasks") or []
        if isinstance(goals, str):
            goals = [goals]
        goals = [str(g).strip() for g in goals if str(g).strip()]
        # 子任务目标里的 wxid 换成人名——子代理和用户都不该看机器标识
        goals = [_humanize_text(self.ds, g) for g in goals]

        if not goals:
            return json.dumps({"分支数": 0, "说明": "没有给出子任务"},
                              ensure_ascii=False), []

        ok, why = self.team.can_delegate(0)
        if not ok:
            return json.dumps({"分支数": 0, "说明": why},
                              ensure_ascii=False), []

        def _on_child_step(step):
            """子代理的步骤也推给前端——用户要看得到「它在查什么」。

            同时收进主代理的轨迹（带 branchId 标记），
            否则用户看整体轨迹时会发现中间少了一截。
            """
            run.steps.append(step)
            # 编号要全局连续：子代理自己从 1 开始会跟主代理的撞号，
            # 前端 v-for 用 :key="s.index"，撞号会导致渲染错乱。
            step["index"] = len(run.steps)
            step["branchStep"] = True
            if on_step:
                try:
                    on_step(step)
                except Exception:
                    pass

        try:
            branches = self.team.delegate(goals, depth=0, parent=None,
                                          on_step=_on_child_step)
        except Exception as e:  # noqa: BLE001 —— 委派失败不能让整个洞察崩掉
            return json.dumps({"分支数": 0,
                               "说明": f"派发失败：{type(e).__name__}: {e}"},
                              ensure_ascii=False), []

        payload = json.dumps(branch_payload(branches), ensure_ascii=False,
                             default=str)
        # 有界：父代理的上下文不能被回填撑爆
        if len(payload) > 6000:
            payload = payload[:5900] + "…（子代理结果过长已截断）"
        return payload, branches


# ------------------------------------------------------------------ 辅助

_TOOL_LABELS = {
    "resolve_time": "换算时间",
    "find_contact": "找人",
    "list_sessions": "列会话",
    "chat_stats": "查统计",
    "search_messages": "搜消息",
    "read_timeline": "读聊天记录",
    "session_detail": "看关系画像",
    "my_profile": "看我的画像",
    "search_moments": "翻朋友圈",
    "delegate_tasks": "派子代理并行查",
}


# 委派工具的 schema。**刻意不放进 agent_tools.TOOL_SCHEMAS**——
# 那样它会出现在 ToolBox.schemas() 里，而 ToolBox.schemas() 正是
# 子代理的工具集来源。子代理能再派子代理 = 无限递归 = 烧钱无底洞。
# 放在这里单独注入，主代理才有这个能力。
DELEGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_tasks",
        "description": "把问题拆成几个互不依赖的子任务，派给子代理**并行**去查。"
                       "适合「分别分析张三和李四王五」「同时查三个时间段」这类"
                       "拆开查比一个个查快的场景。"
                       "每个子任务都要能独立查证。子代理查完会把结论和依据给你，"
                       "你**核对**之后自己整合，不要原样照抄。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "子任务列表，2-4 个。每个写清要查什么、"
                                   "查谁、查哪段时间。",
                },
            },
            "required": ["tasks"],
        },
    },
}


def _humanize_text(ds, text: str) -> str:
    """把文本里的 wxid 机器标识换成人名。

    为什么需要：主代理查完 find_contact 后，手里拿的是 sessionId（wxid），
    它写子任务目标时可能直接把 wxid 抄进去。
    后果有二：
      1. 子代理看到的是机器标识，判断「这是谁」全靠再查一次；
      2. 前端分支卡片会把 wxid 显示给用户——违反「不给人看机器标识」的纪律。

    已有纪律是「在执行层直接替掉，不指望模型自觉」——这里同理。
    """
    if not text:
        return text
    out = text
    # wxid_xxx / gh_xxx / @chatroom
    for m in set(re.findall(r"(?:wxid_[A-Za-z0-9_-]+|gh_[A-Za-z0-9_-]+|@chatroom)", text)):
        try:
            sess = ds.find_session(m)
        except Exception:
            sess = None
        if sess and sess.get("displayName"):
            out = out.replace(m, sess["displayName"])
    return out


def _branch_evidence(branches: list) -> list[dict]:
    """汇总所有子分支的证据。

    子代理查到的原话要能点开看——所以带进父级的证据池，
    并标注来源分支（`via`），用户能看出「这句是子代理查到的」。
    """
    out: list[dict] = []
    for br in branches:
        for ev in (br.evidence or []):
            item = dict(ev)
            item["via"] = br.goal
            item["branchId"] = br.id
            out.append(item)
    return out


def _tool_label(name: str, args: dict) -> str:
    """给人看的一行说明：调用了哪个工具、查了什么。"""
    base = _TOOL_LABELS.get(name, name)
    bits = []
    if name == "resolve_time":
        bits.append(args.get("phrase") or "")
    elif name == "find_contact":
        bits.append(args.get("query") or "")
    elif name == "search_messages":
        bits.append(f"「{args.get('keyword') or ''}」")
    elif name == "read_timeline":
        bits.append(args.get("sessionId") or "")
    elif name == "session_detail":
        bits.append(args.get("sessionId") or "")
    elif name == "chat_stats":
        bits.append(args.get("metric") or "")
    elif name == "search_moments":
        if args.get("keyword"):
            bits.append(f"「{args['keyword']}」")
    elif name == "delegate_tasks":
        tasks = args.get("tasks") or []
        if isinstance(tasks, str):
            tasks = [tasks]
        bits.append(f"{len(tasks)} 个子任务")
    if args.get("from"):
        bits.append(f"起 {args['from']}")
    if args.get("to"):
        bits.append(f"止 {args['to']}")
    tail = " ".join(str(b) for b in bits if b)
    return f"{base} {tail}".strip()


def _tool_payload(res: dict) -> str:
    """工具结果回喂给模型的内容。

    刻意**不塞完整原始数据**——那会让上下文爆炸。只给摘要 + 裁剪后的 data。
    证据（evidence）在前端单独展示，不重复喂给模型（模型从 data 里已经能看到内容）。
    """
    out = {"摘要": res.get("summary") or "", "数据": res.get("data") or {}}
    txt = json.dumps(out, ensure_ascii=False, default=str)
    # 硬上限：单次工具结果不超过 4000 字符，超了截断并说明
    if len(txt) > 4000:
        txt = txt[:3900] + "…（结果过长已截断，需要更细请用更精确的参数再查一次）"
    return txt


def _tool_payload_safe(res: dict) -> str:
    """同 _tool_payload，但截断时明确告知「字段缺失」。

    为什么要多这一层：截断后模型看到的是**残缺 JSON**，
    它会把读不到的字段自己填成 0 或空——实测模型报
    「myMessages: 0、myShare: 0」就是这么来的（真实值是 19719 和 0.081）。
    明确说「这是不完整数据、缺失字段别自己补」，模型就不太会瞎填。
    """
    out = {"摘要": res.get("summary") or "", "数据": res.get("data") or {}}
    txt = json.dumps(out, ensure_ascii=False, default=str)
    if len(txt) > 4000:
        txt = (txt[:3700] + "…（结果过长已截断。"
               "⚠️ 上面是**不完整**的数据，被截掉的字段**不要自己填 0 或猜测**，"
               "需要那些数字就用更精确的参数重新查一次。）")
    return txt


# ------------------------------------------------------------------ 数字审计

# 答案里出现这些数字不算「没有出处」——它们是年份、月份、百分比之类
# 结构性数字，不是从数据里统计出来的量。
_NUM_ALLOW = {
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12",
    "100", "24", "60", "2024", "2025", "2026",
}


def _numbers_in(text: str) -> set[str]:
    """抽出文本里的所有数字（去掉千分位逗号）。"""
    out = set()
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", text or ""):
        s = m.group(0).replace(",", "")
        # 去掉尾部小数点残留
        if s.endswith("."):
            s = s[:-1]
        if s:
            out.add(s)
    return out


def _has_source(n: str, sources: set[str]) -> bool:
    """判断答案里的数字 n 是否能在工具结果里找到出处。

    宽松匹配：2904 ← 2904 / 2,904 / 2904.0 都算找到；
    90 ← 90 / 90.0 / 90.00；以及 2814 的整数形式匹配。
    """
    if n in sources or n in _NUM_ALLOW:
        return True
    # 去尾零的等价形式
    try:
        f = float(n)
    except ValueError:
        return False
    if f.is_integer():
        if str(int(f)) in sources:
            return True
        # 90% ← 90 出现过就算有出处
        if f == int(f) and int(f) <= 100 and str(int(f)) in sources:
            return True
    # 百分比原值
    if f == int(f):
        for cand in (f"{int(f)}", str(int(f))):
            if cand in sources:
                return True
    # 1 位小数的匹配：2814.0 ← 2814
    if f == int(f) and str(int(f)) in sources:
        return True
    return False


# 数字后面跟这些字 → 是日期/时刻/编号，不是统计量
_DATE_SUFFIX = "年月日号点时分秒周季"


def _is_structural(text: str, n: str) -> bool:
    """判断答案里的这个数字是不是日期/时刻的一部分。

    「10 月 27 日」「凌晨 3 点」里的数字不是从数据库统计出来的，
    不该被当成「模型自己算数」。

    注意：只豁免**紧跟在日期/时刻字后面**的数字。
    「12 条消息」这种仍要查——条数是统计量。
    """
    rx = re.escape(n) + r"\s*[" + _DATE_SUFFIX + r"]"
    if re.search(rx, text):
        return True
    # ISO 日期 2026-10-27 里的片段
    if re.search(r"\d{4}-\d{1,2}-\d{1,2}", text):
        return True
    # 纯数字被引号包起来当编号（如「第 3 段」）——不豁免，保守报
    return False


def _unsourced_numbers(run: AgentRun) -> list[str]:
    """算出答案里**没有出处**的数字。

    与 _audit_numbers 同口径，但只返回列表、不拼提示文本。
    拆出来的原因：阶段契约（core/pipeline.py）要用这份清单判状态码，
    而提示文本本身含数字（「2904」会被写进提醒里），
    所以**判定必须用原始答案**，不能用拼完提示的答案——否则会自证。
    """
    ans = run.answer or ""
    if not ans.strip():
        return []
    nums = _numbers_in(ans)
    return sorted(
        (n for n in nums
         if not _has_source(n, run.tool_numbers)
         and not _is_structural(ans, n)),
        key=lambda x: (len(x), x),
    )


def _audit_numbers(run: AgentRun) -> str:
    """核对答案里的数字有没有出处。

    背景：实测模型会把工具返回的 2814 条自己改口成 2904 条——
    用户按 2904 去核对，怎么都对不上。这里做一层程序化兜底：
    答案里出现、但工具结果里从来没出现过的数字，挂一条提示。

    **不篡改答案**——模型可能是对的（比如它自己从多处结果推断），
    但用户有权知道哪些数字是需要留意的。
    """
    ans = run.answer or ""
    if not ans.strip():
        return ans
    unknown = _unsourced_numbers(run)
    # 最多报 5 个，多了是噪音
    if not unknown:
        return ans
    shown = "、".join(unknown[:5])
    more = f"（等 {len(unknown)} 处）" if len(unknown) > 5 else ""
    note = (f"\n\n---\n\n⚠️ 核对提醒：答案里的 **{shown}**{more} "
            f"在工具返回结果里没找到出处，可能是模型自己算的。"
            f"请按上面的依据自己复核一下。")
    return ans + note
