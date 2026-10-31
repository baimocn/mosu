# -*- coding: utf-8 -*-
"""洞察问答 —— 以大模型为核心，从聊天数据里回答问题。

设计要点（2026-11-03）：

  旧形态：GUI 画一堆统计图表，用户自己看图找结论。
  新形态：用户提问 → 本模块采集「可核对的事实」+「语料样本」→ LLM 转述成答案。

为什么不让 LLM 直接看原始消息：
  1) 24 万条消息塞不进上下文，且模型会自己算数（编数字）
  2) 所以数字全部由本模块从 SQL 侧算好，标好口径，交给模型**转述**
  3) 语料检索样本的作用是「让模型体会这个人的说话风格」，
     而不是「给模型答案素材」——风格锚点，不是事实来源

因此答案分三段：
  - 结论（模型写的）
  - 依据（事实清单，数字都带口径，可回溯到接口）
  - 样本（检索到的真实消息片段，标注是谁在什么时候说的）
"""
from __future__ import annotations

import re
from pathlib import Path

# ------------------------------------------------------------------ 预设问题

PRESETS = [
    {
        "id": "who_matters",
        "q": "我这一年里，真正聊得来的人是谁？",
        "hint": "按消息量、主动性、回复速度综合判断",
    },
    {
        "id": "my_talk",
        "q": "我的说话习惯是什么样的？",
        "hint": "从高频词、消息长度、发起对话的倾向看",
    },
    {
        "id": "rhythm",
        "q": "我是个什么作息的人？",
        "hint": "看活跃时段、深夜活跃度、周内分布",
    },
    {
        "id": "topics",
        "q": "这一年我主要在关心什么？",
        "hint": "话题分布 + 时间上的变化",
    },
    {
        "id": "changes",
        "q": "我和以前比有什么变化？",
        "hint": "消息量、主动性、话题重心的前后对比",
    },
]


# ------------------------------------------------------------------ 工具

def _fmt_int(x) -> str:
    try:
        return f"{int(x):,}"
    except Exception:
        return str(x)


def _pct(x) -> str:
    try:
        return f"{float(x) * 100:.1f}%"
    except Exception:
        return "无数据"


# ------------------------------------------------------------------ 事实采集

def collect_facts(ds, session: str | None = None,
                  limit_contacts: int = 8) -> dict:
    """从库里采集客观事实。返回 {facts: [...], metrics: {...}}。

    所有数字都带口径说明，LLM 只允许转述、不允许另算。
    口径来源是 core/analytics.py 的真实返回结构，别凭印象写字段名。
    """
    from core.analytics import Analytics

    facts: list[str] = []
    metrics: dict = {}
    an = Analytics(ds)

    # ---- 全库概况（global_stats → totalMessages/totalSessions/totalContacts）
    g = an.global_stats()
    metrics["totalMessages"] = g.get("totalMessages")
    metrics["totalSessions"] = g.get("totalSessions")
    metrics["totalContacts"] = g.get("totalContacts")
    facts.append(
        f"全库共 {_fmt_int(g.get('totalMessages'))} 条消息、"
        f"{_fmt_int(g.get('totalSessions'))} 个会话、"
        f"{_fmt_int(g.get('totalContacts'))} 个联系人"
    )
    tr = g.get("timeRange") or {}
    if tr.get("firstText"):
        facts.append(f"记录时间跨度：{tr['firstText']} 到 {tr['lastText']}")
    by = g.get("byType") or {}
    if by:
        kn = {"text": "文本", "image": "图片", "voice": "语音", "video": "视频",
              "file": "文件", "emoji": "表情", "system": "系统", "other": "其他"}
        top = sorted(by.items(), key=lambda kv: -kv[1])[:4]
        facts.append("消息构成：" + "、".join(
            f"{kn.get(k, k)} {_fmt_int(v)}" for k, v in top))

    # ---- 活跃节奏（time_stats(by='hour') → distribution 键是 '00'~'23'）
    try:
        hd = an.time_stats(by="hour").get("distribution") or {}
        items = [(int(k), v) for k, v in hd.items() if str(k).isdigit()]
        if items:
            peak_h, peak_n = max(items, key=lambda kv: kv[1])
            metrics["peakHour"] = peak_h
            metrics["peakHourCount"] = peak_n
            facts.append(f"消息最密集的时段是 {peak_h:02d} 点（{_fmt_int(peak_n)} 条）")
            total = sum(v for _, v in items) or 1
            night = sum(v for h, v in items if h >= 22 or h <= 5)
            share = night / total
            metrics["nightShare"] = round(share, 4)
            metrics["nightMessages"] = night
            facts.append(
                f"深夜（22:00–06:00）消息 {_fmt_int(night)} 条，占 {_pct(share)}")
    except Exception:
        pass

    # ---- 说话习惯（本人）：ProfileDistiller.self_profile
    try:
        from core.profile import ProfileDistiller
        sp = ProfileDistiller(ds).self_profile()
        mine = sp.get("myMessages") or 0
        total = sp.get("totalMessages") or 0
        if mine:
            metrics["myMessages"] = mine
            metrics["myShare"] = round(mine / max(1, total), 4)
            facts.append(
                f"你发出 {_fmt_int(mine)} 条，占全部消息的 "
                f"{_pct(mine / max(1, total))}")
    except Exception:
        pass

    # ---- 关系（可选：聚焦某个会话）
    if session:
        try:
            st = an.session_stats(session)
            name = st.get("displayName") or session
            total = st.get("totalMessages") or 0
            sent = st.get("sentMessages") or 0
            recv = st.get("receivedMessages") or 0
            if total:
                metrics["sessionId"] = session
                metrics["sessionName"] = name
                metrics["sessionTotal"] = total
                metrics["sessionSent"] = sent
                facts.append(
                    f"与「{name}」的往来：共 {_fmt_int(total)} 条，"
                    f"你发 {_fmt_int(sent)} 条（{_pct(sent / max(1, total))}），"
                    f"对方发 {_fmt_int(recv)} 条，覆盖 {st.get('activeDays')} 天")
        except Exception:
            pass

    # ---- 联系人排行（contact_stats → {"contacts":[{wxid,displayName,messageCount}]}）
    try:
        contacts = (an.contact_stats(top=limit_contacts) or {}).get("contacts") or []
        if contacts:
            metrics["topContacts"] = [
                {"name": c.get("displayName") or c.get("wxid"),
                 "total": c.get("messageCount") or 0,
                 "wxid": c.get("wxid")}
                for c in contacts
            ]
            facts.append("消息量最多的联系人：" + "、".join(
                f"{c.get('displayName') or c.get('wxid')} "
                f"{_fmt_int(c.get('messageCount') or 0)} 条"
                for c in contacts))
    except Exception:
        pass

    # ---- 高频词（keyword_stats → {"keywords":[{word,count}]}）
    try:
        kw = an.keyword_stats(top=20).get("keywords") or []
        words = [f"{k.get('word')}({_fmt_int(k.get('count'))})"
                 for k in kw[:12] if k.get("word")]
        if words:
            metrics["keywords"] = words
            facts.append("高频词：" + "、".join(words))
    except Exception:
        pass

    return {"facts": facts, "metrics": metrics}


# ------------------------------------------------------------------ 语料样本

def retrieve_samples(corpus_path: str | Path, query: str, topk: int = 8) -> list[dict]:
    """检索语料样本作为「风格锚点」。

    刻意不给模型「答案素材」——只给语气样本。模型要引用具体消息时，
    由样本里的 sender/time 字段指明出处。
    """
    p = Path(corpus_path)
    if not p.is_file():
        return []
    try:
        from core.bot.retriever import StyleRetriever
        r = StyleRetriever(p)
        hits = r.search(query, topk=topk)
    except Exception:
        return []
    out = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        out.append({
            "text": (h.get("text") or "")[:180],
            "speaker": h.get("speaker") or h.get("sender") or "未知",
            "time": h.get("time") or h.get("ts") or "",
        })
    return out


# ------------------------------------------------------------------ 提示词

SYSTEM = """你是一个基于用户本机微信聊天数据库的分析助手。

你的数据来源有两类，**必须严格区分**：

1. 「已核对事实」——从数据库直接算出来的数字，口径已经标好。
   这些数字可以直接引用，但**绝不允许自己另算或改口径**。
   如果事实里没有某个数字，就说「数据里没有这一项」，不要编。

2. 「语料样本」——这个人的真实聊天片段。
   它们的作用是让你体会这个人的说话习惯、语气、用词偏好。
   **不是**让你从中找答案。如果要引用某句话，必须带上它的说话人和时间。

写作要求：
- 开头一句话直接给结论，不要「根据数据来看」这种铺垫
- **每得出一个结论，紧接着用「（依据：<事实里的一句>）」把依据标出来**，
  依据要照抄事实清单里的原话，不要自己改写数字
- 至少标出 3 处依据；事实清单里有的项都要用上
- 口语化，像朋友之间分析事情，不要写成报告腔
- 有矛盾或反直觉的地方要指出来（比如「消息最多的人其实不是你最主动找的人」）
- 不确定的地方明确说不确定
- 不要罗列所有数字，只挑最能说明问题的几个

全文用中文。"""

USER_TMPL = """问题：{question}

已核对事实：
{facts}

语料样本（仅作风格参考，不是答案素材）：
{samples}

请基于以上信息回答。"""


def build_prompt(question: str, facts: list[str], samples: list[dict]) -> list[dict]:
    fs = "\n".join(f"- {f}" for f in facts) if facts else "（本次没有采集到可用事实）"
    if samples:
        ss = "\n".join(
            f"- [{s.get('speaker', '?')} {s.get('time', '')}] {s.get('text', '')}"
            for s in samples)
    else:
        ss = "（无样本）"
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER_TMPL.format(
            question=question, facts=fs, samples=ss)},
    ]


# ------------------------------------------------------------------ 回答解析

_CITE = re.compile(r"[（(]\s*(?:依据|来自|见)\s*[:：]?\s*([^）)]{2,120})[）)]")


def split_citations(answer: str) -> dict:
    """从答案里抽出「依据」片段。

    模型可能把依据写在正文里（（依据：…））。这里只做**抽取与归位**，
    不改写内容。
    """
    cites = []
    for m in _CITE.finditer(answer or ""):
        t = m.group(1).strip()
        if t and t not in cites:
            cites.append(t)
    return {"citations": cites}


# ------------------------------------------------------------------ 主入口

def ask(ds, question: str, session: str | None = None,
        corpus_path: str | Path | None = None,
        topk: int = 8) -> dict:
    """问一句，返回 {answer, facts, samples, citations, metrics}。"""
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")

    fb = collect_facts(ds, session=session)
    samples: list[dict] = []
    if corpus_path:
        samples = retrieve_samples(corpus_path, question, topk=topk)

    messages = build_prompt(question, fb["facts"], samples)

    from core.bot.llm import LlmClient
    llm = LlmClient()
    answer = llm.chat(messages, temperature=0.7, max_tokens=1200)

    out = split_citations(answer)
    return {
        "question": question,
        "answer": answer,
        "citations": out["citations"],
        "facts": fb["facts"],
        "samples": samples,
        "metrics": fb["metrics"],
        "model": llm.model,
    }
