# -*- coding: utf-8 -*-
"""墨溯 Agent 工具层 —— 把已有的数据能力包装成大模型能调的工具。

为什么要有这层（2026-11-03）：
    旧洞察是「固定管道」——预先塞 7 条事实 + 8 条语料样本，模型只能转述。
    问题是模型被绑住了手：问「我和张三上个月聊了什么」，它答不了，
    因为张三的聊天记录压根没进上下文。
    现在改成 Agentic：模型自己决定调哪个工具、查哪段时间、哪个会话。

与竞品 CipherTalk 的差异（有意为之，不是遗漏）：
  - 竞品有 30+ 工具（记忆、语音转写、图片、插件、文件…），墨溯只做**只读数据查询**
  - 竞品的向量检索靠 embedding 模型；墨溯**不依赖 embedding**（零外部依赖铁律），
    用「关键词检索 + 精确时间范围 + 会话定向」三件套覆盖主要查询
  - 竞品有 query_sql 兜底（模型写 SQL）；墨溯**不给模型写 SQL 的能力**——
    数字只能来自本模块已算好的结果，杜绝「模型自己编一个数」

每个工具返回统一结构：
    {"summary": "给模型看的一句话摘要", "data": {...结构化...}, "evidence": [...]}
evidence 是**出处**：每条都带 sessionId/localId/time/sender，
前端可以据此做「点依据跳到原文」，模型也可以据此标注引用。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from core.locks import DS_LOCK

# ------------------------------------------------------------------ 时间解析


def parse_when(s: str | None, end: bool = False) -> int | None:
    """把「上个月」「2025-08-01」「昨天」这类说法解析成时间戳（秒）。

    模型给的日期说法五花八门，这里做宽松解析：
      - 空 → None（不限制）
      - YYYY-MM-DD / YYYY-MM / YYYY / YYYY年M月 / M月D日
      - 今天/昨天/前天/本周/上周/这个月/上个月/今年/去年
      - 「最近N天」「近N周」
    end=True 时，「上个月」这类区间取的是**区间末尾**（用于 to_ts）。
    """
    if not s:
        return None
    t = str(s).strip()
    if not t:
        return None
    now = datetime.now()
    d0 = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def mk(y, m, d, hh=0, mm=0, ss=0):
        try:
            return int(datetime(y, m, d, hh, mm, ss).timestamp())
        except Exception:
            return None

    # ---- 相对日
    rel = {"今天": 0, "今日": 0, "昨天": -1, "昨日": -1, "前天": -2}
    for k, off in rel.items():
        if k in t:
            day = d0 + timedelta(days=off)
            if end:
                return int((day + timedelta(days=1)).timestamp()) - 1
            return int(day.timestamp())

    # ---- 近 N 天/周
    m = re.search(r"(?:最近|近|过去)\s*(\d+)\s*(天|日|周|个?月)", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        days = n * 7 if "周" in unit else (n * 30 if "月" in unit else n)
        if end:
            return int(now.timestamp())
        return int((d0 - timedelta(days=days)).timestamp())

    # ---- 明确月份区间
    if "上个月" in t or "上月" in t:
        y, mth = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        if end:
            # 上个月**的最后一天**，不是上上个月的最后一天
            if mth == 12:
                return mk(y, 12, 31, 23, 59, 59)
            return mk(y, mth + 1, 1) - 1
        return mk(y, mth, 1)
    if "这个月" in t or "本月" in t:
        if end:
            return int(now.timestamp())
        return mk(now.year, now.month, 1)
    if "去年" in t:
        if end:
            return mk(now.year - 1, 12, 31, 23, 59, 59)
        return mk(now.year - 1, 1, 1)
    if "今年" in t:
        if end:
            return int(now.timestamp())
        return mk(now.year, 1, 1)
    if "上周" in t or "上个星期" in t:
        base = d0 - timedelta(days=7)
        if end:
            # 上周的最后一天（周日）
            last_sun = base + timedelta(days=(6 - base.weekday()))
            return int((last_sun + timedelta(days=1)).timestamp()) - 1
        return int((base - timedelta(days=base.weekday())).timestamp())
    if "本周" in t or "这周" in t or "这个星期" in t:
        if end:
            return int(now.timestamp())
        return int((d0 - timedelta(days=d0.weekday())).timestamp())

    # ---- 具体日期
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if end:
            return mk(y, mo, d, 23, 59, 59)
        return mk(y, mo, d)
    # 年月（2026-09）：end 要的是**这个月的月末**，不是上个月末。
    # 之前写成 mk(y, mo, 1) - 1，算出来是上月最后一天——整个时间范围错位一个月。
    m = re.search(r"(\d{4})[-/年](\d{1,2})(?:月)?(?!\d)", t)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12):
            return None
        if end:
            if mo == 12:
                return mk(y, 12, 31, 23, 59, 59)
            return mk(y, mo + 1, 1) - 1
        return mk(y, mo, 1)
    m = re.search(r"(\d{4})", t)
    if m:
        y = int(m.group(1))
        if end:
            return mk(y, 12, 31, 23, 59, 59)
        return mk(y, 1, 1)

    # ---- 只说了月份，没给年份：「10月」「10月份」
    # 默认取**最近的那个** 10 月：今年 10 月还没到就用去年 10 月。
    # 实测踩过：模型自己把「10 月份」算成去年 10 月（现在是 2026-11，应指 2026-10），
    # 整条答案全错，用户还看不出哪里错了。
    m = re.search(r"(\d{1,2})\s*月(?:份)?", t)
    if m:
        mo = int(m.group(1))
        if not (1 <= mo <= 12):
            return None
        y = now.year if mo <= now.month else now.year - 1
        if end:
            if mo == 12:
                return mk(y, 12, 31, 23, 59, 59)
            return mk(y, mo + 1, 1) - 1
        return mk(y, mo, 1)

    return None


def _ts_text(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _clip(s: str, n: int = 160, keep_newline: bool = False) -> str:
    """截断长文本。keep_newline=True 时保留换行（给「展开看原文」用）。"""
    s = (s or "").strip()
    if not keep_newline:
        s = s.replace("\n", " ")
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------ 工具注册表

# OpenAI function calling 的 schema 格式。描述写给模型看——要写清边界，
# 让模型知道什么时候该用哪个工具、以及每个工具「管什么不管什么」。
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "resolve_time",
            "description": "把「上个月」「去年」「最近三个月」「2025-08」这类时间说法"
                           "换算成绝对日期区间。**用户问的时间范围必须先用它换算**，"
                           "不要自己凭印象猜月份——猜错整个答案就废了。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string", "description": "时间说法，如 上个月/最近30天/今年/2025-08"},
                    "end": {"type": "boolean", "description": "true=取区间末尾，false=取区间开头"},
                },
                "required": ["phrase"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_name",
            "description": "把会话 ID（wxid 或 @chatroom 形式）换成人名。"
                           "会话 ID 是机器标识，用户看不懂；写答案前用它换成名字。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sessionId": {"type": "string", "description": "会话 ID"},
                },
                "required": ["sessionId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_contact",
            "description": "把人名或群名解析成会话 ID。先用它拿到 sessionId，再传给其他工具。"
                           "支持模糊匹配（部分姓名即可）。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "人名或群名，可只写一部分"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_sessions",
            "description": "列出最近活跃的会话（联系人/群），可按关键字过滤。"
                           "用来了解「有哪些聊天对象」。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "可选，按名称过滤"},
                    "limit": {"type": "integer", "description": "返回条数，默认 20，最大 50"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chat_stats",
            "description": "纯统计：数量、排名、分布、时间跨度。回答「有多少条」「谁聊得最多」"
                           "「什么时候最活跃」「都是什么类型的消息」这类问题时用这个。"
                           "**数字必须来自本工具，不要自己数、不要自己算百分比。**只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "enum": ["overview", "ranking", "time_distribution", "keywords", "types"],
                        "description": "overview=总览与时间跨度；ranking=联系人消息量排行；"
                                       "time_distribution=按小时/星期/月分布（用 by 指定）；"
                                       "keywords=高频词；types=消息类型构成",
                    },
                    "by": {"type": "string", "enum": ["hour", "weekday", "month", "day"],
                           "description": "time_distribution 的分组维度，默认 hour"},
                    "sessionId": {"type": "string", "description": "可选，限定某个会话"},
                    "from": {"type": "string", "description": "起始时间。可写绝对日期（2025-08-01）"
                                                        "或相对说法（上个月/最近30天/今年），系统会换算"},
                    "to": {"type": "string", "description": "结束时间，同 from"},
                    "top": {"type": "integer", "description": "ranking/keywords 的取前几名，默认 10"},
                },
                "required": ["metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": "按关键词检索聊天原文。回答「谁提过某件事」「含某个词的消息」"
                           "「某天聊了什么」这类需要看原始内容的问题时用这个。"
                           "可限定会话与时间范围。返回命中消息原文（带时间与发送者）。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要搜的词或短语"},
                    "sessionId": {"type": "string", "description": "可选，限定某个会话"},
                    "from": {"type": "string", "description": "起始时间"},
                    "to": {"type": "string", "description": "结束时间"},
                    "limit": {"type": "integer", "description": "返回条数，默认 20，最大 50"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_timeline",
            "description": "读取某个会话在一段时间内的连续消息原文（按时间顺序）。"
                           "适合「某人某天聊了什么」「把这段讲清楚」。"
                           "拿到 sessionId 后用这个读原文。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sessionId": {"type": "string", "description": "会话 ID（先用 find_contact 拿）"},
                    "from": {"type": "string", "description": "起始时间"},
                    "to": {"type": "string", "description": "结束时间"},
                    "limit": {"type": "integer", "description": "返回条数，默认 40，最大 120"},
                },
                "required": ["sessionId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_detail",
            "description": "单个会话的关系画像：往来总量、我发/对方发比例、活跃天数、"
                           "共同话题、首次与最近一次联系。回答「我和某人关系怎么样」时用。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sessionId": {"type": "string", "description": "会话 ID"},
                    "from": {"type": "string", "description": "可选，起始时间"},
                    "to": {"type": "string", "description": "可选，结束时间"},
                },
                "required": ["sessionId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "my_profile",
            "description": "我本人的说话习惯画像：消息量、平均长度、高频词、发起对话的比例、"
                           "活跃时段分布。回答「我平时怎么说话」「我是什么作息」时用。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": "可选，起始时间"},
                    "to": {"type": "string", "description": "可选，结束时间"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_moments",
            "description": "检索朋友圈动态（只读）。回答「某人发过什么朋友圈」"
                           "「朋友圈里提到 X」「那段时间朋友圈内容」。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "可选，正文关键词"},
                    "author": {"type": "string", "description": "可选，作者 wxid 或名字片段"},
                    "from": {"type": "string", "description": "可选，起始时间"},
                    "to": {"type": "string", "description": "可选，结束时间"},
                    "limit": {"type": "integer", "description": "默认 15，最大 40"},
                },
            },
        },
    },
]


class ToolBox:
    """工具执行器。持 Dataset，按名字分发到具体实现。

    所有工具都是只读的：不写库、不改文件、不碰微信进程。
    """

    def __init__(self, ds):
        self.ds = ds
        self._sessions_cache: list[dict] | None = None

    # ------------------------------------------------------------ 基础设施
    def _sessions(self) -> list[dict]:
        if self._sessions_cache is None:
            self._sessions_cache = self.ds.sessions()
        return self._sessions_cache

    def _resolve(self, session_id: str | None):
        """sessionId → 会话 dict。找不到返回 None。"""
        if not session_id:
            return None
        s = self.ds.find_session(session_id)
        return s

    def _range(self, args: dict) -> tuple[int | None, int | None]:
        return parse_when(args.get("from")), parse_when(args.get("to"), end=True)

    # ------------------------------------------------------------ 工具实现
    def t_resolve_time(self, args: dict) -> dict:
        phrase = (args.get("phrase") or "").strip()
        end = bool(args.get("end"))
        ts = parse_when(phrase, end=end)
        if ts is None:
            return {"summary": f"没听懂「{phrase}」指哪段时间",
                    "data": {"phrase": phrase}, "evidence": []}
        d = datetime.fromtimestamp(ts)
        return {"summary": f"「{phrase}」→ {d.strftime('%Y-%m-%d %H:%M')}",
                "data": {"phrase": phrase, "timestamp": ts,
                         "date": d.strftime("%Y-%m-%d %H:%M")},
                "evidence": []}

    def t_find_contact(self, args: dict) -> dict:
        q = (args.get("query") or "").strip()
        if not q:
            return {"summary": "没给查询词", "data": {"matches": []}, "evidence": []}
        hits = self.ds.find_sessions(q)
        matches = [
            {"sessionId": s["sessionId"], "displayName": s["displayName"],
             "type": s.get("type"), "messageCount": s.get("messageCount")}
            for s in hits[:10]
        ]
        if not matches:
            return {"summary": f"没找到叫「{q}」的会话",
                    "data": {"matches": []}, "evidence": []}
        lines = "、".join(f"{m['displayName']}（{m['messageCount']} 条）" for m in matches[:5])
        return {"summary": f"找到 {len(matches)} 个匹配：{lines}",
                "data": {"matches": matches}, "evidence": []}

    def t_list_sessions(self, args: dict) -> dict:
        q = (args.get("query") or "").strip().lower()
        limit = max(1, min(50, int(args.get("limit") or 20)))
        sess = self._sessions()
        if q:
            sess = [s for s in sess if q in (s.get("displayName") or "").lower()]
        sess = sorted(sess, key=lambda s: s.get("messageCount") or 0, reverse=True)[:limit]
        rows = [{"sessionId": s["sessionId"], "displayName": s["displayName"],
                 "type": s.get("type"), "messageCount": s.get("messageCount")} for s in sess]
        return {"summary": f"共 {len(rows)} 个会话：" + "、".join(
            f"{r['displayName']}({r['messageCount']})" for r in rows[:10]),
            "data": {"sessions": rows}, "evidence": []}

    def t_chat_stats(self, args: dict) -> dict:
        from core.analytics import Analytics
        metric = (args.get("metric") or "overview").lower()
        by = (args.get("by") or "hour").lower()
        top = max(1, min(50, int(args.get("top") or 10)))
        f_ts, t_ts = self._range(args)
        sess_id = args.get("sessionId")
        an = Analytics(self.ds)

        if metric == "overview":
            g = an.global_stats(from_ts=f_ts, to_ts=t_ts)
            tr = g.get("timeRange") or {}
            total = g.get("totalMessages") or 0
            mine = g.get("myMessages") or 0
            ev = []
            # 摘要必须带「我发出多少」：只给总量的话，模型问不到这个数，
            # 只能去猜或者告诉用户「查不到」——实测它就答过「没法保证准确，不瞎报」。
            summary = (f"共 {total} 条消息（其中我发出 {mine} 条，占 "
                       f"{round(mine / max(1, total) * 100, 1)}%）、"
                       f"{g.get('totalSessions')} 个会话、"
                       f"{g.get('totalContacts')} 个联系人；"
                       f"时间跨度 {tr.get('firstText')} 到 {tr.get('lastText')}")
            return {"summary": summary,
                    "data": {"totalMessages": total,
                             "myMessages": mine,
                             "myShare": g.get("myShare"),
                             "totalSessions": g.get("totalSessions"),
                             "totalContacts": g.get("totalContacts"),
                             "timeRange": tr,
                             "byType": g.get("byType")},
                    "evidence": ev}

        if metric == "ranking":
            st = an.contact_stats(top=top, from_ts=f_ts, to_ts=t_ts)
            contacts = st.get("contacts") or []
            lines = "、".join(
                f"{c.get('displayName') or c.get('wxid')} {c.get('messageCount')} 条"
                for c in contacts[:10])
            # 排行结论也要能核对：每条一个 evidence（无原文，但有口径与数字）
            # kind="stat" —— 它是统计数字不是原话，前端要分开摆，
            # 不能让用户以为「消息量 158565 条」是某句话
            ev = [{"sessionId": c.get("wxid"), "localId": None,
                   "time": "", "sender": c.get("displayName") or c.get("wxid"),
                   "kind": "stat",
                   "text": f"消息量 {c.get('messageCount')} 条"}
                  for c in contacts[:10]]
            return {"summary": f"消息量排行：{lines}",
                    "data": {"contacts": contacts}, "evidence": ev}

        if metric == "time_distribution":
            # session 必须传下去——漏传会让「某人的时段分布」返回全库分布，
            # 模型会拿一个完全不相干的数字当结论（端到端实测踩到过）。
            st = an.time_stats(by=by, session=sess_id, from_ts=f_ts, to_ts=t_ts)
            dist = st.get("distribution") or {}
            items = sorted(((str(k), v) for k, v in dist.items()),
                           key=lambda kv: -kv[1])
            if not items:
                return {"summary": "该时间段没有消息", "data": {"distribution": {}}, "evidence": []}
            peak = items[0]
            total = sum(v for _, v in items) or 1
            ev = [{"sessionId": None, "localId": None, "time": str(peak[0]),
                   "sender": "统计", "kind": "stat", "text": f"{peak[1]} 条"}]
            return {"summary": (f"按{by}分布，共 {total} 条；峰值 {peak[0]} 有 {peak[1]} 条"
                                f"（占 {round(peak[1] / total * 100, 1)}%）"),
                    "data": {"by": by, "distribution": dist, "total": total},
                    "evidence": ev}

        if metric == "keywords":
            st = an.keyword_stats(session=sess_id, top=top, from_ts=f_ts, to_ts=t_ts)
            kws = st.get("keywords") or []
            return {"summary": "高频词：" + "、".join(
                f"{k.get('word')}({k.get('count')})" for k in kws[:12]),
                "data": {"keywords": kws}, "evidence": []}

        if metric == "types":
            # 有 sessionId 就必须走单会话口径——
            # global_stats 只支持全库，传了 sessionId 还返回全库数字，
            # 模型会拿不相干的数字当这个人的消息类型构成
            # （端到端实测：子代理自己发现了这个不一致）。
            if sess_id:
                ss = an.session_stats(sess_id, from_ts=f_ts, to_ts=t_ts)
                by_type = ss.get("byType") or {}
                scope = (f"会话「{ss.get('session', {}).get('displayName') or sess_id}」")
            else:
                g = an.global_stats(from_ts=f_ts, to_ts=t_ts)
                by_type = g.get("byType") or {}
                scope = "全库"
            return {"summary": f"{scope}消息类型：" + "、".join(
                f"{k} {v}" for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])),
                "data": {"byType": by_type, "scope": scope}, "evidence": []}

        return {"summary": f"未知统计维度 {metric}", "data": {}, "evidence": []}

    def t_search_messages(self, args: dict) -> dict:
        kw = (args.get("keyword") or "").strip()
        if not kw:
            return {"summary": "没给关键词", "data": {"messages": []}, "evidence": []}
        limit = max(1, min(50, int(args.get("limit") or 20)))
        f_ts, t_ts = self._range(args)
        try:
            r = self.ds.search(kw, session=args.get("sessionId"), limit=limit,
                               from_ts=f_ts, to_ts=t_ts)
        except SystemExit as e:
            return {"summary": f"搜索失败：{e}", "data": {"messages": []}, "evidence": []}
        msgs = r.get("messages") or []
        out, ev = [], []
        for m in msgs:
            txt = _clip(m.get("content") or "", 200)
            out.append({"time": _ts_text(m.get("createTime")),
                        "sender": m.get("senderName") or "",
                        "direction": m.get("direction"),
                        "sessionName": m.get("sessionName"),
                        "content": txt})
            ev.append({"sessionId": m.get("sessionId"), "localId": m.get("localId"),
                       "time": _ts_text(m.get("createTime")),
                       "sender": m.get("senderName") or "",
                       "kind": "quote",
                       "text": txt,
                       # full 是完整原文（evidence 不回喂给模型，所以放长一点无代价）。
                       # 前端折叠时显示 text，展开时显示 full —— 这才是「可核对」。
                       "full": _clip(m.get("content") or "", 1200, keep_newline=True)})
        total = r.get("total")
        summary = f"命中 {total} 条「{kw}」" + (f"，展示前 {len(out)} 条" if out else "")
        return {"summary": summary, "data": {"total": total, "messages": out}, "evidence": ev}

    def t_read_timeline(self, args: dict) -> dict:
        sid = args.get("sessionId")
        sess = self._resolve(sid)
        if not sess:
            return {"summary": f"找不到会话 {sid}", "data": {"messages": []}, "evidence": []}
        limit = max(1, min(120, int(args.get("limit") or 40)))
        f_ts, t_ts = self._range(args)
        try:
            r = self.ds.messages(sess, limit=limit, order="asc",
                                 from_ts=f_ts, to_ts=t_ts)
        except SystemExit as e:
            return {"summary": f"读取失败：{e}", "data": {"messages": []}, "evidence": []}
        msgs = r.get("messages") or []
        out, ev = [], []
        for m in msgs:
            txt = _clip(m.get("content") or "", 200)
            who = "我" if m.get("direction") == "out" else (m.get("senderName") or "对方")
            out.append({"time": _ts_text(m.get("createTime")), "who": who,
                        "content": txt})
            ev.append({"sessionId": m.get("sessionId"), "localId": m.get("localId"),
                       "time": _ts_text(m.get("createTime")), "sender": who,
                       "kind": "quote", "text": txt,
                       "full": _clip(m.get("content") or "", 1200, keep_newline=True)})
        return {"summary": (f"「{sess.get('displayName')}」在 "
                            f"{_ts_text(f_ts) if f_ts else '全部时间'}～"
                            f"{_ts_text(t_ts) if t_ts else '现在'} "
                            f"共读到 {len(out)} 条消息"),
                "data": {"sessionId": sess["sessionId"],
                         "displayName": sess.get("displayName"),
                         "messages": out},
                "evidence": ev}

    def t_session_detail(self, args: dict) -> dict:
        sid = args.get("sessionId")
        sess = self._resolve(sid)
        if not sess:
            return {"summary": f"找不到会话 {sid}", "data": {}, "evidence": []}
        f_ts, t_ts = self._range(args)
        from core.analytics import Analytics
        try:
            st = Analytics(self.ds).session_stats(sess["sessionId"], from_ts=f_ts, to_ts=t_ts)
        except SystemExit as e:
            return {"summary": f"统计失败：{e}", "data": {}, "evidence": []}
        name = st.get("displayName") or sess.get("displayName")
        total = st.get("totalMessages") or 0
        sent = st.get("sentMessages") or 0
        recv = st.get("receivedMessages") or 0
        first = st.get("firstMessageText")
        last = st.get("lastMessageText")
        summary = (f"与「{name}」共 {total} 条消息，我发 {sent} 条（{round(sent / max(1, total) * 100)}%）、"
                   f"对方发 {recv} 条，活跃 {st.get('activeDays')} 天"
                   + (f"；首次 {first} 最近 {last}" if first else ""))
        # 画像里的数字也得能核对——模型拿着「2904 条」写进答案，
        # 用户却看不到出处，就等于信它瞎报。这里逐项产出 stat 类证据。
        ev = [{"sessionId": sess["sessionId"], "localId": None, "time": "",
               "sender": name, "kind": "stat",
               "text": f"共 {total} 条，我发 {sent} 条（{round(sent / max(1, total) * 100)}%），"
                       f"对方发 {recv} 条，活跃 {st.get('activeDays')} 天"}]
        return {"summary": summary, "data": st, "evidence": ev}

    def t_resolve_name(self, args: dict) -> dict:
        """把 wxid / sessionId 换成人名。

        工具回喂给模型的 sessionId 是 wxid（如 wxid_t5hxszuom64e22），
        模型拿去搜就搜不到；但反过来，**模型在轨迹里看到裸 wxid，
        用户也看不懂**。这个工具让模型能把 id 翻译成名字再写进答案。
        """
        sid = (args.get("sessionId") or "").strip()
        if not sid:
            return {"summary": "没给 sessionId", "data": {}, "evidence": []}
        sess = self._resolve(sid)
        if not sess:
            return {"summary": f"找不到会话 {sid}", "data": {}, "evidence": []}
        name = sess.get("displayName") or sid
        try:
            from core.analytics import Analytics
            st = Analytics(self.ds).session_stats(sid)
            total = st.get("totalMessages") or 0
        except Exception:
            total = sess.get("messageCount") or 0
        return {"summary": f"{sid} 是「{name}」（共 {total} 条消息）",
                "data": {"sessionId": sid, "displayName": name, "totalMessages": total},
                "evidence": [{"sessionId": sid, "localId": None, "time": "",
                              "sender": name, "kind": "stat",
                              "text": f"「{name}」共 {total} 条消息"}]}

    def t_my_profile(self, args: dict) -> dict:
        f_ts, t_ts = self._range(args)
        try:
            from core.profile import ProfileDistiller
            p = ProfileDistiller(self.ds).self_profile(from_ts=f_ts, to_ts=t_ts)
        except Exception as e:  # noqa: BLE001
            return {"summary": f"画像失败：{e}", "data": {}, "evidence": []}
        # self_profile 的计数在 overview 下，不是顶层（别照印象写 p["myMessages"]）
        ov = p.get("overview") or {}
        mine = ov.get("myMessages") or 0
        total = ov.get("totalMessages") or 0
        rhythm = p.get("rhythm") or {}
        style = p.get("style") or {}
        # rhythm.peakHour 是**全库**峰值（total 维度），不是「我」的峰值。
        # 要说「我几点话最多」，得从 hourly 里按 mine 另算一次。
        hourly = rhythm.get("hourly") or []
        my_peak = max(hourly, key=lambda x: x.get("mine") or 0, default=None)
        my_peak_n = (my_peak.get("mine") or 0) if my_peak else 0
        bits = [f"我发出 {mine} 条，占全部 {total} 条的 "
                f"{round(mine / max(1, total) * 100, 1)}%"]
        if my_peak:
            bits.append(f"**我自己**发得最多的整点是 {my_peak.get('hour')} 点"
                        f"（{my_peak_n} 条）")
        if style.get("avgLength"):
            bits.append(f"我发出的消息平均 {style['avgLength']} 字")
        if rhythm.get("nightMessages"):
            bits.append(f"凌晨 0–5 点我发了 {rhythm['nightMessages']} 条")
        # 回喂给模型的是**精选字段**，不是全量画像。
        # 全量 p 有近 4000 字符，会把 _tool_payload 顶到 4000 上限被截断——
        # 截断后模型看到的是残缺 JSON，缺的字段它会自己填 0
        # （实测：模型报「myMessages: 0、myShare: 0」就是这么来的）。
        # 摘要里已经含全部关键数字，这里只补结构化的那几项。
        slim = {
            "overview": ov,
            "style": {k: style.get(k) for k in
                      ("avgLength", "emojiRate", "questionRate", "byKind")},
            "rhythm": {k: rhythm.get(k) for k in ("nightMessages", "peakHour")
                       if rhythm.get(k) is not None},
            "insights": (p.get("insights") or [])[:8],
        }
        return {"summary": "；".join(bits), "data": slim, "evidence": []}

    def t_search_moments(self, args: dict) -> dict:
        kw = (args.get("keyword") or "").strip()
        limit = max(1, min(40, int(args.get("limit") or 15)))
        f_ts, t_ts = self._range(args)
        try:
            from core.sns import Moments
            mo = Moments(self.ds)
            r = mo.entries(limit=limit, from_ts=f_ts, to_ts=t_ts)
        except Exception as e:  # noqa: BLE001
            return {"summary": f"朋友圈读取失败：{e}", "data": {"entries": []}, "evidence": []}
        entries = r.get("entries") or []
        author = (args.get("author") or "").strip()
        if author:
            entries = [e for e in entries
                       if author.lower() in (e.get("author", {}).get("displayName") or "").lower()
                       or author == e.get("author", {}).get("wxid")]
        if kw:
            entries = [e for e in entries if kw in (e.get("content") or "")]
        out = []
        for e in entries:
            out.append({"author": (e.get("author") or {}).get("displayName"),
                        "time": _ts_text(e.get("createTime")),
                        "content": _clip(e.get("content") or "", 200)})
        return {"summary": f"读到 {len(out)} 条朋友圈",
                "data": {"entries": out}, "evidence": []}

    # ------------------------------------------------------------ 分发
    _TABLE = {
        "resolve_time": t_resolve_time,
        "find_contact": t_find_contact,
        "list_sessions": t_list_sessions,
        "chat_stats": t_chat_stats,
        "search_messages": t_search_messages,
        "read_timeline": t_read_timeline,
        "session_detail": t_session_detail,
        "resolve_name": t_resolve_name,
        "my_profile": t_my_profile,
        "search_moments": t_search_moments,
    }

    def _humanize(self, args: dict) -> dict:
        """把参数里的 sessionId（wxid）换成人名。

        不指望模型自觉调 resolve_name——**在执行层直接替掉**，
        否则轨迹里到处是 wxid_t5hxszuom64e22 这种机器标识，
        用户看着头疼，模型引用时也容易写成 id 而不是名字。
        """
        out = dict(args)
        sid = args.get("sessionId")
        if sid and isinstance(sid, str) and sid.startswith(("wxid_", "gh_", "@")):
            sess = self.ds.find_session(sid)
            if sess and sess.get("displayName"):
                out["_sessionName"] = sess["displayName"]
                out["sessionId"] = sess["displayName"]
        return out

    def run(self, name: str, args: dict | None = None) -> dict:
        fn = self._TABLE.get(name)
        if fn is None:
            return {"summary": f"没有工具 {name}", "data": {}, "evidence": []}
        args = args or {}
        t0 = time.time()
        try:
            # 全局数据锁：并行子代理会共用同一个 SQLite 连接，
            # 同一时刻只能有一个线程在用（游标会互相踩，
            # 表现为结果错乱或 database disk image is malformed）。
            # 工具调用是毫秒级，串行化不损失什么；真正该并行的 LLM 推理在锁外。
            with DS_LOCK:
                r = fn(self, args)
        except Exception as e:  # noqa: BLE001 —— 工具失败必须变成可读信息，不能崩
            r = {"summary": f"工具 {name} 执行失败：{type(e).__name__}: {e}",
                 "data": {}, "evidence": []}
        r.setdefault("data", {})
        r.setdefault("evidence", [])
        r["tool"] = name
        # 回喂给模型的参数也要人名版，否则模型会把 id 当成名字念出来
        r["args"] = self._humanize(args)
        r["cost"] = round(time.time() - t0, 2)
        return r

    @staticmethod
    def schemas(names: list[str] | None = None) -> list[dict]:
        """工具 schema。传 names 则只返回这些工具（子代理用受限工具集）。"""
        if names is None:
            return TOOL_SCHEMAS
        want = set(names)
        return [s for s in TOOL_SCHEMAS
                if (s.get("function") or {}).get("name") in want]
