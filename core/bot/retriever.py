# -*- coding: utf-8 -*-
"""语料检索器 —— 为 bot 提供"风格参考样本"。

消融实验结论（scripts/ablation_retrieval.py，2026-10-18）：
  同义词扩展与场景加权对 Recall@5 的贡献为 0.000（0.072 vs 0.072），
  已按奥卡姆剃刀删除。词重叠在短文本语料上 Recall@5 约 7%——
  检索样本的定位因此明确为"风格锚点"（让模型体会语气），
  不是"找相似问题的答案"；主题相关性由端到端消融进一步验证
  （相关检索 vs 随机抽样，见 scripts/ablation_e2e.py）。
"""
from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

STOP = set("的了是在我你他她这那就都也和有不吧啊哦呢吗什么怎么一个可以现在时候知道因为所以但是然后如果还是这个那个觉得感觉有点一下今天明天昨天")


class StyleRetriever:
    """jieba 分词倒排 + 词重叠排序。样本只作风格锚点，不当答案。"""

    def __init__(self, corpus_path: str | Path, max_len: int = 200,
                 seed: int | None = None):
        import logging
        import warnings
        logging.getLogger("jieba").setLevel(logging.ERROR)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import jieba
        self._jieba = jieba
        self._rng = random.Random(seed)
        self.corpus: list[dict] = []
        with open(corpus_path, encoding="utf-8") as f:
            for line in f:
                try:
                    c = json.loads(line)
                except Exception:
                    continue
                if c.get("kind") == "text" and 2 <= (c.get("len") or 0) <= max_len:
                    self.corpus.append(c)
        self.inv: dict[str, list[int]] = {}
        for i, c in enumerate(self.corpus):
            for w in set(self._tok(c["text"])):
                self.inv.setdefault(w, []).append(i)

    def _tok(self, text: str) -> list[str]:
        return [w for w in self._jieba.cut(text)
                if len(w) >= 2 and w not in STOP and not w.isascii()]

    def search(self, query: str, topk: int = 5) -> list[dict]:
        """词重叠检索 top-k。"""
        qw = set(self._tok(query))
        if not qw or not self.corpus:
            return []
        scores: Counter = Counter()
        for w in qw:
            for i in self.inv.get(w, []):
                scores[i] += 1
        qn = len(qw) ** 0.5
        out = []
        for i, _ in scores.most_common(topk * 6):
            c = self.corpus[i]
            cw = set(self._tok(c["text"]))
            if not cw:
                continue
            overlap = len(qw & cw)
            if overlap == 0:
                continue
            sim = overlap / (qn * len(cw) ** 0.5)
            out.append((sim, c))
        out.sort(key=lambda x: -x[0])
        return [c for _, c in out[:topk]]

    def sample(self, topk: int = 5) -> list[dict]:
        """随机抽样（消融对照：不追求主题相关的纯风格样本）。"""
        if not self.corpus:
            return []
        return self._rng.sample(self.corpus, min(topk, len(self.corpus)))
