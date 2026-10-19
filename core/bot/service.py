# -*- coding: utf-8 -*-
"""BotService —— 编排：检索 → 组装 → 生成 → 校验 → 重试。

组件全部构造函数注入（依赖倒置），测试可替换任意组件：
    BotService(corpus, retriever=..., checker=..., llm=..., history_limit=...)
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

SYSTEM_TMPL = """你是白墨本人在微信聊天时的数字分身。规则（来自他 {n} 条真实发言的统计）：

【怎么说话 —— 这部分必须遵守】
1. 默认短句：平均 12 字、七成消息在 10 字以内。**需要解释清楚的话题允许说长**，
   但要保持一句一句的口语短句，绝不写成段落文章
2. 标点：多数消息不打标点；感叹号几乎不用
3. 表情：约三成消息带内嵌表情，常用 [旺柴][捂脸][便便][皱眉][苦涩]——自然点缀，别每条都加
4. 口头禅：应该、直接、不用、不然、主要、好像——自然使用，别堆砌
5. 应答习惯：常用「好」「ok」「对」「行」「嗯」开头
6. 绝对不要出现「很高兴为您」「综上所述」「作为AI」这类助手腔

【说什么 —— 语料不是你的知识边界】
7. 下面给的语料片段**只是风格样本，不代表你能聊的话题范围**。
   任何话题（工作、技术、生活、决策、闲聊）都可以用你自己的知识来回答，
   只是要用白墨的口气说出来。
8. 真的不知道就直说"不知道"，别编造事实；但不要因为"语料里没提过"就回避问题。"""

REF_TMPL = """下面是从白墨历史微信发言里检索到的 {n} 条片段（按与当前话题的相关度排序）。
它们只是**风格参考**——用来体会他的语气、用词、表情习惯。
**不要照抄这些原句**，要根据当前对话上下文组织一条新的回复。
如果参考片段与当前话题不完全匹配，忽略片段内容、只取语气。
注意：片段里没出现过的话题不代表不能聊，只管按他的口气回答。

参考片段：
{blocks}"""

FACTS_HEADER = """【本次可引用的数据库事实】
{facts}
"""

PERSONA_HEADER = """【你现在扮演的角色：{name}——{tagline}】
{prompt}

【风格要求】
{style_hint}

【红线】
{guard}

【长度】
回复不超过 {max_words} 字，宁可少说也不要写长段。"""


def _norm_key(query: str) -> str:
    """问题归一化：去空白/标点/大小写。用于缓存键——
    「在吗」「在吗？」「在 吗」视为同一个问题。"""
    return re.sub(r"[\s\W_]+", "", query).lower()


def _cache_key(query: str, persona: str) -> str:
    """人格进缓存键：同一个问题在不同人格下答案完全不同，不能共用。"""
    return f"{persona}:{_norm_key(query)}"


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
        # ---- 人格：决定角色、语气与能看到的数据 ----
        from core.bot import personas as P
        self.personas = P
        self.persona_id = P.DEFAULT_PERSONA
        self.ds = None                     # 注入 Dataset 后，分析者/安慰者才有事实
        # ---- 语义缓存：高频短问题（问候/确认类）命中直接回，省额度省延迟 ----
        # 边界从严：只缓存「通过校验且 <= cache_max_len 字」的回复——
        # 长回复往往依赖上下文，缓存会露馅；MOSU_BOT_CACHE=0 可关
        self.cache_limit = 200               # FIFO 容量
        self.cache_max_len = 20              # 超过此长度的回复不缓存
        self.cache_enabled = os.environ.get("MOSU_BOT_CACHE", "1") != "0"
        self._cache: dict[str, dict] = {}

    def _cache_put(self, key: str, reply: str, retrieved: list[dict],
                   passed: bool) -> None:
        if (not self.cache_enabled or not passed
                or not (0 < len(reply) <= self.cache_max_len)):
            return
        self._cache[key] = {
            "reply": reply,
            "retrieved": [{"text": c["text"], "session": c.get("session", "")}
                          for c in retrieved],
        }
        while len(self._cache) > self.cache_limit:      # FIFO 淘汰最旧
            self._cache.pop(next(iter(self._cache)))

    def _record_history(self, conv_id: str | None, query: str, reply: str) -> None:
        cid = conv_id or "default"
        hist = self.history.setdefault(cid, [])
        hist.extend([{"role": "user", "content": query},
                     {"role": "assistant", "content": reply}])
        if len(hist) > self.history_limit * 2:
            del hist[:-self.history_limit * 2]

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

    def _facts_for(self, p, query: str) -> str:
        """给「能看数据」的人格拼一段可引用的事实；没有 Dataset 就返回空。"""
        if p.data == "none" or self.ds is None:
            return ""
        try:
            from core.bot.facts import FactBuilder
            return FactBuilder(self.ds, session_hint=query).build(p.data)
        except Exception:
            return ""

    def _build_messages(self, query: str, retrieved: list[dict],
                        conv_id: str | None, persona=None) -> list[dict]:
        n_corpus = len(getattr(self.retriever, "corpus", []) or [])
        p = persona or self.personas.get(self.persona_id)
        system = SYSTEM_TMPL.format(n=f"{n_corpus:,}" if n_corpus else "上万")
        # 模仿者本身就是「数字分身」，角色段会与之重复；其余角色才需要追加
        if p.id != "mimic":
            system += "\n\n" + PERSONA_HEADER.format(
                name=p.name, tagline=p.tagline, prompt=p.persona_prompt,
                style_hint=p.style_hint, guard=p.guard, max_words=p.max_words)
        msgs = [{"role": "system", "content": system}]

        facts = self._facts_for(p, query)
        if facts:
            msgs.append({"role": "user", "content": FACTS_HEADER.format(facts=facts)})
            msgs.append({"role": "assistant",
                         "content": "记住了，只引用上面给出的事实，不自己编数字。"})

        if retrieved and p.id == "mimic":
            blocks = "\n".join(f"{i}. {c['text']}" for i, c in enumerate(retrieved, 1))
            msgs.append({"role": "user",
                         "content": REF_TMPL.format(n=len(retrieved), blocks=blocks)})
            msgs.append({"role": "assistant", "content": "知道了，我按这个语气回复，不照抄。"})
        for h in self.history.get(conv_id or "", [])[-self.history_limit:]:
            msgs.append(h)
        msgs.append({"role": "user", "content": query})
        return msgs

    def chat(self, query: str, conv_id: str | None = None, topk: int = 5,
             persona: str | None = None) -> dict:
        t0 = time.time()
        p = self.personas.get(persona or self.persona_id)
        key = _cache_key(query, p.id)

        # ---- 缓存命中：0 LLM 调用，0 延迟 ----
        if self.cache_enabled and key in self._cache:
            hit = self._cache[key]
            self._record_history(conv_id, query, hit["reply"])
            return {
                "reply": hit["reply"],
                "passed": True,
                "problems": [],
                "tries": 0,
                "retrieved": hit["retrieved"],
                "cost": round(time.time() - t0, 3),
                "cached": True,
                "persona": p.id,
            }

        # 事实查询已内置在 _build_messages 里，这里不再单独拼装
        retrieved = self._retrieve(query, topk) if p.id == "mimic" else []
        messages = self._build_messages(query, retrieved, conv_id, persona=p)
        reply, problems, tries = "", [], 0
        for attempt in range(self.max_retries + 1):
            tries = attempt + 1
            reply = self.llm.chat(messages)
            ok, problems = self.checker.check(reply, retrieved)
            if ok:
                break
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": self.checker.feedback(problems)})
        self._record_history(conv_id, query, reply)
        self._cache_put(key, reply, retrieved, passed=not problems)
        return {
            "reply": reply,
            "passed": not problems,
            "problems": problems,
            "tries": tries,
            "retrieved": [{"text": c["text"], "session": c.get("session", "")}
                          for c in retrieved],
            "cost": round(time.time() - t0, 2),
            "cached": False,
            "persona": p.id,
        }

    def set_persona(self, persona_id: str | None):
        """切换默认人格，返回被选中的人格对象。

        未知 id **保持当前人格不变**——静默改回默认会让人以为自己换成功了。
        """
        if persona_id and persona_id in self.personas.PERSONAS:
            self.persona_id = persona_id
        return self.personas.get(self.persona_id)

    def preview_prompt(self, query: str, topk: int = 5,
                       persona: str | None = None) -> dict:
        """不调 LLM，只看 prompt 组装（调试/审查用）。"""
        p = self.personas.get(persona or self.persona_id)
        retrieved = self._retrieve(query, topk) if p.id == "mimic" else []
        return {"messages": self._build_messages(query, retrieved, None, persona=p),
                "retrieved": retrieved,
                "persona": p.id}
