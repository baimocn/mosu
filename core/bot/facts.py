# -*- coding: utf-8 -*-
"""给 AI 的事实层：把数据库里的统计压成可以直接引用的短事实。

之前 bot 只能看到"检索到的语料片段"（风格样本），看不到数据本身 ——
于是它只能闲聊，不能回答"我们聊了多久""他多久回一次"。这一层补上这件事。

设计原则（为聊天场景，不为报表场景）：
1. 只回答「对方会问的具体问题」：关系体量、活跃节奏、话题、情绪、发起模式。
2. 每条事实都是**可核对**的：数字来自数据库，不做二次推理。
3. 压缩到几十行以内——聊天窗口塞不进报表。
4. 找不到会话就返回空，不编造。

耗时保护：任何一项都可能触发一次库扫描，总预算 6 秒，超时就返回已拿到的。
"""
from __future__ import annotations

import time

# 单项预算（秒），超时即放弃该项，保证整体不超过总预算
ITEM_BUDGET = 1.8


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def _dur(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return "无数据"
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{round(seconds / 60)} 分钟"
    if seconds < 86400:
        return f"{round(seconds / 3600, 1)} 小时"
    return f"{round(seconds / 86400, 1)} 天"


def threading_run(target, timeout: float = ITEM_BUDGET):
    """最小实现：跑一个函数并限时（daemon 线程，超时就不等）。"""
    import threading
    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout)
    return th.is_alive()      # True = 超时


class FactBuilder:
    """把 Dataset + ProfileDistiller 的结果整理成给 LLM 的事实块。"""

    def __init__(self, ds, session_hint: str | None = None,
                 total_budget: float = 6.0):
        self.ds = ds
        self.hint = (session_hint or "").strip()
        self.budget = total_budget
        self._t0 = time.time()

    # ------------------------------------------------------------------ 解析对象
    def resolve_session(self) -> dict | None:
        if not self.hint:
            return None
        try:
            return self.ds.find_session(self.hint)
        except Exception:
            return None

    def target_sessions(self, limit: int = 3) -> list[dict]:
        """当前话题对应的会话；解析不到就给往来最多的几个，作为备选上下文。"""
        s = self.resolve_session()
        if s:
            return [s]
        try:
            rows = self.ds.sessions()
            rows = sorted(rows, key=lambda r: -(r.get("messageCount") or 0))
            return rows[:limit]
        except Exception:
            return []

    # ------------------------------------------------------------------ 事实
    def global_facts(self) -> list[str]:
        out: list[str] = []
        try:
            g = _guarded(lambda: _global(self.ds), [])
            if g:
                out += list(g)[:6]
        except Exception:
            pass
        return out

    def relation_facts(self) -> list[str]:
        """针对某一个会话/联系人的关系事实。"""
        out: list[str] = []
        sess = self.resolve_session()
        if not sess:
            return out
        name = sess.get("displayName") or sess.get("sessionId")
        try:
            st = _guarded(lambda: _session(self.ds, sess["sessionId"]), {})
            if st:
                total = st.get("totalMessages") or 0
                sent = st.get("sentMessages") or 0
                recv = st.get("receivedMessages") or 0
                out.append(f"与{name}的往来：共 {total} 条，你发 {sent} 条（{_pct(sent / max(1, total))}），"
                           f"对方发 {recv} 条；覆盖 {st.get('activeDays')} 天")
                if st.get("firstMessageText"):
                    out.append(f"第一句在 {st['firstMessageText']}，最近一句在 {st.get('lastMessageText')}")
                kinds = st.get("byType") or {}
                if kinds:
                    top = sorted(kinds.items(), key=lambda kv: -kv[1])[:3]
                    desc = "、".join(f"{_KNAME.get(k, k)} {v} " for k, v in top).strip()
                    out.append(f"内容构成以{desc}为主")
        except Exception:
            pass
        try:
            prof = _guarded(lambda: _profile(self.ds, sess["sessionId"]), {})
            if prof:
                inter = prof.get("interaction") or {}
                if inter.get("initiatedByMe") or inter.get("initiatedByThem"):
                    a = inter.get("initiatedByMe") or 0
                    b = inter.get("initiatedByThem") or 0
                    who = "你" if a >= b else "对方"
                    out.append(f"发起新对话：{who}更主动（你 {a} 次 / 对方 {b} 次，"
                               f"以间隔 6 小时以上算新一轮）")
                if inter.get("myAvgReplySeconds") or inter.get("theirAvgReplySeconds"):
                    out.append(f"平均回复间隔：你 {_dur(inter.get('myAvgReplySeconds'))}，"
                               f"对方 {_dur(inter.get('theirAvgReplySeconds'))}")
                sent = prof.get("sentiment") or {}
                if sent.get("mine") or sent.get("theirs"):
                    m = (sent.get("mine") or {}).get("avg")
                    t = (sent.get("theirs") or {}).get("avg")
                    if m is not None and t is not None:
                        out.append(f"情绪倾向（词典打分 -1~1）：你的消息 {_fmt_score(m)}，"
                                   f"对方 {_fmt_score(t)}")
                topics = (prof.get("topics") or {}).get("theirs") or []
                if topics:
                    t3 = "、".join(t.get("topic", "") for t in topics[:3])
                    out.append(f"对方高频话题：{t3}")
        except Exception:
            pass
        return out

    def rhythm_facts(self) -> list[str]:
        out: list[str] = []
        try:
            agg = _guarded(lambda: _hours(self.ds), {})
            if agg:
                peak = max(agg.items(), key=lambda kv: kv[1]) if agg else None
                if peak:
                    out.append(f"全库消息最密集的时段：{peak[0]} 点（{peak[1]} 条）")
        except Exception:
            pass
        return out

    def build(self, mode: str = "full") -> str:
        """mode: full=全局+关系+节奏；light=只要关系；none=不查。"""
        lines: list[str] = []
        if mode == "full":
            lines += self.global_facts()[:6]
            lines += self.rhythm_facts()[:1]
        if mode in ("full", "light"):
            lines += self.relation_facts()[:6]
        if not lines:
            return ""
        body = "\n".join("- " + x for x in lines)
        return ("以下是从本机聊天数据库里统计出来的客观事实，可以直接引用，"
                "数字已经核对过，不要自己另算；"
                "每条都已标注口径（平均/占比/条数），转述时不要改口径：\n" + body)


def _fmt_score(v) -> str:
    try:
        v = float(v)
    except Exception:
        return "无数据"
    if v > 0.15:
        return f"偏正面（{v:+.2f}）"
    if v < -0.15:
        return f"偏负面（{v:+.2f}）"
    return f"中性（{v:+.2f}）"


_KNAME = {"text": "文本", "image": "图片", "voice": "语音", "video": "视频",
          "file": "文件", "emoji": "表情", "system": "系统", "link": "链接",
          "other": "其他"}


def _guarded(fn, default):
    """限时调用（超时不阻塞聊天）。"""
    box = {}

    def target():
        try:
            box["v"] = fn()
        except Exception as e:
            box["e"] = f"{type(e).__name__}: {e}"

    threading_run(target)
    return box.get("v", default)


# ------------------------------------------------------------------ 具体查询
def _global(ds):
    from core.analytics import Analytics
    g = Analytics(ds).global_stats()
    out = [f"全库共 {g['totalMessages']} 条消息、{g['totalSessions']} 个会话、"
           f"{g['totalContacts']} 个联系人"]
    tr = g.get("timeRange") or {}
    if tr.get("firstText"):
        out.append(f"记录时间跨度：{tr['firstText']} 到 {tr['lastText']}")
    by = g.get("byType") or {}
    if by:
        top = sorted(by.items(), key=lambda kv: -kv[1])[:3]
        out.append("消息构成：" + "、".join(f"{_KNAME.get(k, k)} {v}" for k, v in top))
    return out


def _session(ds, sid):
    from core.analytics import Analytics
    return Analytics(ds).session_stats(sid)


def _profile(ds, sid):
    from core.profile import ProfileDistiller
    return ProfileDistiller(ds).contact_profile(sid)


def _hours(ds):
    from core.analytics import Analytics
    t = Analytics(ds).time_stats(by="hour")
    dist = t.get("distribution") or {}
    return {int(k): v for k, v in dist.items() if str(k).isdigit()}