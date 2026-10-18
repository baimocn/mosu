# -*- coding: utf-8 -*-
"""消融实验：RAG 检索质量（纯离线，不需要 LLM）

问题：StyleRetriever 里的同义词扩展（SYNONYMS）和场景加权（SCENE_HINTS）
到底有没有提升检索质量？没有就删（奥卡姆剃刀）。

方法（留一评估）：
  - 从语料里按会话分组，取每条白墨发言作为 query
  - ground truth = 同一会话、时间相邻 ±2h 的其他发言（真实的对话上下文）
  - 指标：Recall@5（top5 命中 ground truth 的比例）、MRR
  - 变体：A 当前版（同义词+场景加权）/ B 纯词重叠 / C 词重叠+同义词（无场景加权）

用法：python scripts/ablation_retrieval.py [corpus.jsonl] [--n 500]
"""
from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.bot import SCENE_HINTS, SYNONYMS, STOP  # noqa: E402


class RetrieverVariant:
    """可参数化的检索器，用于消融。"""

    def __init__(self, corpus: list[dict], use_synonyms: bool = True,
                 use_scene: bool = True):
        import logging
        import warnings
        logging.getLogger("jieba").setLevel(logging.ERROR)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import jieba
        self._jieba = jieba
        self.corpus = corpus
        self.use_synonyms = use_synonyms
        self.use_scene = use_scene
        self.inv: dict[str, list[int]] = {}
        for i, c in enumerate(corpus):
            for w in set(self._tok(c["text"])):
                self.inv.setdefault(w, []).append(i)

    def _tok(self, text: str) -> list[str]:
        return [w for w in self._jieba.cut(text)
                if len(w) >= 2 and w not in STOP and not w.isascii()]

    def _expand(self, query: str) -> set[str]:
        words = set(self._tok(query))
        if not self.use_synonyms:
            return words
        extra = set()
        for w in list(words):
            for key, syns in SYNONYMS.items():
                if w == key or key in query:
                    extra.update(syns)
                    extra.add(key)
        return words | {w for w in extra if len(w) >= 2}

    def _scene(self, query: str) -> str | None:
        if not self.use_scene:
            return None
        for scene, kws in SCENE_HINTS.items():
            if any(k in query for k in kws):
                return scene
        return None

    def search(self, query: str, topk: int = 5) -> list[int]:
        qw = self._expand(query)
        if not qw:
            return []
        scene = self._scene(query)
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
            if scene == "tech" and any(k in c["text"] for k in ("AI", "模型", "部署", "代码", "服务器")):
                sim *= 1.3
            elif scene == "food" and any(k in c["text"] for k in ("吃", "饭", "外卖", "奶茶")):
                sim *= 1.3
            elif scene == "study" and any(k in c["text"] for k in ("课", "作业", "考试", "图书馆")):
                sim *= 1.3
            out.append((sim, i))
        out.sort(key=lambda x: -x[0])
        return [i for _, i in out[:topk]]


def load_corpus(path: str | Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                c = json.loads(line)
            except Exception:
                continue
            if c.get("kind") == "text" and 2 <= (c.get("len") or 0) <= 200:
                out.append(c)
    out.sort(key=lambda c: c.get("ts") or 0)
    return out


def build_ground_truth(corpus: list[dict], window: int = 7200) -> dict[int, set[int]]:
    """每条语料的 ground truth = 同会话 ±2h 的其他发言。"""
    by_sess: dict[str, list[int]] = {}
    for i, c in enumerate(corpus):
        by_sess.setdefault(c["session"], []).append(i)
    gt: dict[int, set[int]] = {}
    for sess, idxs in by_sess.items():
        for pos, i in enumerate(idxs):
            t = corpus[i].get("ts") or 0
            s = set()
            for j in idxs:
                if j == i:
                    continue
                if abs((corpus[j].get("ts") or 0) - t) <= window:
                    s.add(j)
            if s:
                gt[i] = s
    return gt


def evaluate(retriever: RetrieverVariant, gt: dict[int, set[int]],
             queries: list[int], topk: int = 5) -> dict:
    hits = 0
    rr = 0.0
    evaluated = 0
    for qi in queries:
        truth = gt.get(qi)
        if not truth:
            continue
        evaluated += 1
        ranked = retriever.search(retriever.corpus[qi]["text"], topk=topk)
        # 排除自身
        ranked = [i for i in ranked if i != qi]
        if any(i in truth for i in ranked):
            hits += 1
        for rank, i in enumerate(ranked, 1):
            if i in truth:
                rr += 1.0 / rank
                break
    n = max(1, evaluated)
    return {"recall@5": round(hits / n, 4), "mrr": round(rr / n, 4),
            "evaluated": evaluated}


def main():
    corpus_path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
        else r"D:\Desktop\分析\_个人bot\corpus.jsonl"
    n = 500
    if "--n" in sys.argv:
        n = int(sys.argv[sys.argv.index("--n") + 1])
    corpus = load_corpus(corpus_path)
    gt = build_ground_truth(corpus)
    print(f"语料 {len(corpus)} 条，有 ground truth 的 {len(gt)} 条")
    random.seed(42)
    queries = random.sample(sorted(gt), min(n, len(gt)))

    variants = {
        "A 当前版（同义词+场景加权）": RetrieverVariant(corpus, True, True),
        "B 纯词重叠（剃刀版）": RetrieverVariant(corpus, False, False),
        "C 同义词无场景加权": RetrieverVariant(corpus, True, False),
    }
    print(f"\n评估 {len(queries)} 个 query（ground truth = 同会话 ±2h 发言）\n")
    results = {}
    for name, r in variants.items():
        t0 = time.time()
        results[name] = evaluate(r, gt, queries)
        results[name]["cost"] = round(time.time() - t0, 1)
        print(f"  {name:<28} Recall@5={results[name]['recall@5']:.3f}  "
              f"MRR={results[name]['mrr']:.3f}  ({results[name]['cost']}s)")
    a = results["A 当前版（同义词+场景加权）"]
    b = results["B 纯词重叠（剃刀版）"]
    delta = a["recall@5"] - b["recall@5"]
    print(f"\n结论：同义词+场景加权相对纯词重叠的 Recall@5 差异 = {delta:+.3f}")
    if abs(delta) < 0.02:
        print("  → 差异不显著（<2%），按奥卡姆剃刀应删除 SYNONYMS/SCENE_HINTS")
    elif delta > 0:
        print("  → 组件有正贡献，保留")
    else:
        print("  → 组件是负贡献，必须删除")


if __name__ == "__main__":
    main()
