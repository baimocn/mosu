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
**「上个月」「去年」「最近三个月」这类说法，你必须自己换算成绝对日期再传给工具**，
不要凭印象猜——本机聊天记录跨 2025-06 到 2026-10，猜错月份整个答案就废了。
换算没把握的时候，先调一次 chat_stats 看时间跨度，再决定查哪一段。

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
        self.answer = ""
        self.tokens = 0
        self.t0 = time.time()
        # 工具返回里出现过的所有数字（字符串形式）——用来核对答案里的数字有没有出处
        self.tool_numbers: set[str] = set()

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "steps": self.steps,
            "evidence": self.evidence,
            "tokens": self.tokens,
            "cost": round(time.time() - self.t0, 2),
        }


class InsightAgent:
    """Agent 引擎。一个实例复用一个 ToolBox（会话列表缓存）。"""

    # 单轮最多几次工具调用。竞品用 maxSteps=10，
    # 这里给 8 次：够覆盖「解析人名 → 查统计 → 读原文」这类三段式链路，
    # 又不至于让用户等太久。
    MAX_STEPS = 8
    # 单次工具调用的超时上限（秒）
    TOOL_TIMEOUT = 45

    def __init__(self, ds, llm=None, max_steps: int | None = None):
        self.ds = ds
        self.box = ToolBox(ds)
        self._llm = llm
        if max_steps:
            self.MAX_STEPS = max_steps

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
        for step_i in range(self.MAX_STEPS):
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

        run.answer = _audit_numbers(run)
        return run


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
}


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
    nums = _numbers_in(ans)
    unknown = sorted(
        (n for n in nums
         if not _has_source(n, run.tool_numbers)
         and not _is_structural(ans, n)),
        key=lambda x: (len(x), x),
    )
    # 最多报 5 个，多了是噪音
    if not unknown:
        return ans
    shown = "、".join(unknown[:5])
    more = f"（等 {len(unknown)} 处）" if len(unknown) > 5 else ""
    note = (f"\n\n---\n\n⚠️ 核对提醒：答案里的 **{shown}**{more} "
            f"在工具返回结果里没找到出处，可能是模型自己算的。"
            f"请按上面的依据自己复核一下。")
    return ans + note
