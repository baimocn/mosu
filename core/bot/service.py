# -*- coding: utf-8 -*-
"""BotService —— 编排：检索 → 组装 → 生成 → 校验 → 重试。

组件全部构造函数注入（依赖倒置），测试可替换任意组件：
    BotService(corpus, retriever=..., checker=..., llm=..., history_limit=...)
"""
from __future__ import annotations

import time
from pathlib import Path

SYSTEM_TMPL = """你是白墨本人在微信聊天时的数字分身。规则（来自他 {n} 条真实发言的统计）：
1. 长度：平均 12 字，71% 的消息在 10 字以内——默认短句，别长篇大论
2. 标点：65% 的消息不打标点；感叹号几乎不用；问号率仅 3%
3. 表情：37% 的消息带内嵌表情，常用 [旺柴][捂脸][便便][皱眉][苦涩]——自然地点缀，不要每条都加
4. 口头禅：应该、直接、不用、不然、主要、好像——自然地用，别堆砌
5. 应答习惯：常用「好」「ok」「对」「行」「嗯」回应
6. 群聊更放松更短，私聊稍长；不确定就直接说不知道，别编
7. 绝对不要出现「很高兴为您」「综上所述」「作为AI」这类助手腔"""

REF_TMPL = """下面是从白墨历史微信发言里检索到的 {n} 条片段（按与当前话题的相关度排序）。
它们只是**风格参考**——用来体会他的语气、用词、表情习惯。
**不要照抄这些原句**，要根据当前对话上下文组织一条新的回复。
如果参考片段与当前话题不完全匹配，忽略片段内容、只取语气。

参考片段：
{blocks}"""


class BotService:
    def __init__(self, corpus_path: str | Path, retriever=None, checker=None,
                 llm=None, max_retries: int = 2, history_limit: int = 20,
                 sample_mode: bool = False):
        """
        sample_mode: True 时用随机抽样代替相关性检索（消融对照用）。
        history_limit: 每个会话保留的最大历史条数（防内存无界增长）。
        """
        if retriever is None:
            from core.bot.retriever import StyleRetriever
            retriever = StyleRetriever(corpus_path)
        if checker is None:
            from core.bot.checker import StyleChecker
            checker = StyleChecker()
        self.retriever = retriever
        self.checker = checker
        self._llm = llm                      # None 则首次用时懒加载
        self.max_retries = max_retries
        self.history_limit = history_limit
        self.sample_mode = sample_mode
        self.history: dict[str, list[dict]] = {}

    @property
    def llm(self):
        if self._llm is None:
            from core.bot.llm import LlmClient
            self._llm = LlmClient()
        return self._llm

    @llm.setter
    def llm(self, value):
        self._llm = value

    def _retrieve(self, query: str, topk: int) -> list[dict]:
        if self.sample_mode:
            return self.retriever.sample(topk)
        return self.retriever.search(query, topk=topk)

    def _build_messages(self, query: str, retrieved: list[dict],
                        conv_id: str | None) -> list[dict]:
        n_corpus = len(getattr(self.retriever, "corpus", []) or [])
        msgs = [{"role": "system",
                 "content": SYSTEM_TMPL.format(n=f"{n_corpus:,}" if n_corpus else "上万")}]
        if retrieved:
            blocks = "\n".join(f"{i}. {c['text']}" for i, c in enumerate(retrieved, 1))
            msgs.append({"role": "user",
                         "content": REF_TMPL.format(n=len(retrieved), blocks=blocks)})
            msgs.append({"role": "assistant", "content": "知道了，我按这个语气回复，不照抄。"})
        for h in self.history.get(conv_id or "", [])[-self.history_limit:]:
            msgs.append(h)
        msgs.append({"role": "user", "content": query})
        return msgs

    def chat(self, query: str, conv_id: str | None = None, topk: int = 5) -> dict:
        t0 = time.time()
        retrieved = self._retrieve(query, topk)
        messages = self._build_messages(query, retrieved, conv_id)
        reply, problems, tries = "", [], 0
        for attempt in range(self.max_retries + 1):
            tries = attempt + 1
            reply = self.llm.chat(messages)
            ok, problems = self.checker.check(reply, retrieved)
            if ok:
                break
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": self.checker.feedback(problems)})
        cid = conv_id or "default"
        hist = self.history.setdefault(cid, [])
        hist.extend([{"role": "user", "content": query},
                     {"role": "assistant", "content": reply}])
        if len(hist) > self.history_limit * 2:
            del hist[:-self.history_limit * 2]
        return {
            "reply": reply,
            "passed": not problems,
            "problems": problems,
            "tries": tries,
            "retrieved": [{"text": c["text"], "session": c.get("session", "")}
                          for c in retrieved],
            "cost": round(time.time() - t0, 2),
        }

    def preview_prompt(self, query: str, topk: int = 5) -> dict:
        """不调 LLM，只看 prompt 组装（调试/审查用）。"""
        retrieved = self._retrieve(query, topk)
        return {"messages": self._build_messages(query, retrieved, None),
                "retrieved": retrieved}
