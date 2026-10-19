# -*- coding: utf-8 -*-
"""端到端消融实验：bot 三组件（RAG / prompt / 校验）各自的贡献。

需要 LLM key（MOSU_BOT_API_KEY）。变体矩阵：

    V1 全裸      ：无 system 风格 prompt、无 RAG、无校验
    V2 仅 prompt ：有 system、无 RAG、无校验
    V3 prompt+RAG：有 system、相关检索样本、无校验
    V4 全组件    ：有 system、相关检索、有校验（当前方案）
    V5 RAG换随机 ：有 system、随机样本（不追求相关）、有校验
    V6 无RAG有校验：有 system、无 RAG、有校验

指标（全部离线计算，不依赖人工标注）：
    1. 长度分布距离：生成回复长度分布 vs 白语气料长度分布（Wasserstein-1）
    2. 表情率差    ：生成带 [xx] 表情的比例 vs 语料的 37%
    3. AI 腔命中率 ：生成回复中 AI_CLICHES 的出现率（应为 0）
    4. 口头禅命中率：应该/直接/不用/不然 等词的出现率 vs 语料
    5. 应答词占比  ：好/ok/对/行/嗯 开头或独立成句的比例
    6. 多样性      ：不同 query 间回复的不重复率（防塌缩到 "ok"）

用法：
    export MOSU_BOT_API_KEY=...
    python scripts/ablation_e2e.py [--n 30] [--corpus path]
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 捕获原始 system 模板，供 build_variant 在变体间正确还原（避免 V1 污染后续变体）
import core.bot.service as _svc
_REAL_SYSTEM_TMPL = _svc.SYSTEM_TMPL

TEST_QUERIES = [
    "明天要不要去图书馆", "这个 bug 怎么解", "客户问报价多少", "晚上吃什么",
    "教程看了没", "在吗", "帮我看看这个报错", "周末有空吗", "部署到哪台服务器",
    "这道题怎么做", "快递到了没", "群里的文档你看了吗", "模型接口调不通",
    "明天几点开工", "食堂哪家好吃", "感谢昨晚帮忙", "红包领了吗", "论文改了没",
    "游戏上号吗", "天气怎么样", "这个方案靠谱吗", "需要我寄东西过去吗",
    "代码提交了没", "明天记得带书", "演唱会门票抢到了", "电脑重装了",
    "帮我砍一刀", "会议改到几点", "暑假回家吗", "论文查重过了吗",
]

AI_CLICHES = ["很高兴为您", "综上所述", "作为AI", "希望这能帮到", "为您服务",
              "首先，", "其次，", "我将", "理解您", "祝您"]
VERBAL = ["应该", "直接", "不用", "不然", "主要", "好像"]
ACK = ["好", "ok", "OK", "对", "行", "嗯", "可以", "收到"]
EMOJI_RE = re.compile(r"\[([一-龥]{1,8})\]")


def wasserstein_1(a: list[float], b: list[float]) -> float:
    """一维 Wasserstein 距离（经验分布）。"""
    if not a or not b:
        return float("inf")
    a_s, b_s = sorted(a), sorted(b)
    all_v = sorted(set(a_s) | set(b_s))
    ca = cb = 0
    ia = ib = 0
    dist = 0.0
    for v in all_v:
        while ia < len(a_s) and a_s[ia] <= v:
            ca += 1
            ia += 1
        while ib < len(b_s) and b_s[ib] <= v:
            cb += 1
            ib += 1
        dist += abs(ca / len(a_s) - cb / len(b_s))
    return dist


def style_metrics(replies: list[str], corpus_lens: list[float],
                  corpus_emoji_rate: float, corpus_verbal_rate: float) -> dict:
    lens = [float(len(r)) for r in replies]
    with_emoji = sum(1 for r in replies if EMOJI_RE.search(r))
    ai_hits = sum(1 for r in replies for w in AI_CLICHES if w in r)
    verbal_hits = sum(1 for r in replies if any(w in r for w in VERBAL))
    ack_hits = sum(1 for r in replies
                   if r.strip() in ACK or any(r.strip().startswith(a) for a in ACK))
    n = max(1, len(replies))
    return {
        "n": len(replies),
        "avgLen": round(sum(lens) / n, 1),
        "lenW1": round(wasserstein_1(lens, corpus_lens), 3),
        "emojiRate": round(with_emoji / n, 3),
        "emojiRateDelta": round(with_emoji / n - corpus_emoji_rate, 3),
        "aiClicheRate": round(ai_hits / n, 3),
        "verbalRate": round(verbal_hits / n, 3),
        "verbalDelta": round(verbal_hits / n - corpus_verbal_rate, 3),
        "ackRate": round(ack_hits / n, 3),
        "uniqueRate": round(len(set(replies)) / n, 3),
    }


def build_variant(name: str, corpus: str, with_prompt: bool, rag: str, check: bool):
    """rag: 'relevant' | 'random' | 'none'"""
    from core.bot.checker import StyleChecker
    from core.bot.retriever import StyleRetriever
    from core.bot.service import BotService

    class _NoChecker:
        def check(self, reply, retrieved=None):
            return True, []

        @staticmethod
        def feedback(problems):
            return "重说"

    import core.bot.service as svc
    # SYSTEM_TMPL 是模块级全局，必须每个变体显式设回，否则 V1（弱 prompt）
    # 会污染后续 V2~V6，导致"仅 prompt / 全组件"的对比失真。
    svc.SYSTEM_TMPL = ("你是白墨的微信聊天分身。" if not with_prompt else
                       _REAL_SYSTEM_TMPL)
    bot = BotService(corpus,
                     retriever=StyleRetriever(corpus),
                     checker=StyleChecker() if check else _NoChecker(),
                     sample_mode=(rag == "random"))
    if rag == "none":
        bot._retrieve = lambda q, k: []
    return bot


def main():
    n = 30
    if "--n" in sys.argv:
        n = int(sys.argv[sys.argv.index("--n") + 1])
    corpus = str(ROOT / "persona" / "corpus.jsonl")
    if "--corpus" in sys.argv:
        corpus = sys.argv[sys.argv.index("--corpus") + 1]

    # 语料基线统计
    corpus_items = []
    with open(corpus, encoding="utf-8") as f:
        for line in f:
            try:
                c = json.loads(line)
            except Exception:
                continue
            if c.get("kind") == "text" and 2 <= (c.get("len") or 0) <= 200:
                corpus_items.append(c)
    corpus_lens = [float(c["len"]) for c in corpus_items]
    corpus_emoji_rate = sum(1 for c in corpus_items if c.get("emojis")) / max(1, len(corpus_items))
    corpus_verbal_rate = sum(1 for c in corpus_items
                             if any(w in c["text"] for w in VERBAL)) / max(1, len(corpus_items))
    print(f"语料基线: n={len(corpus_items)} 表情率={corpus_emoji_rate:.2f} "
          f"口头禅率={corpus_verbal_rate:.2f} 平均长度={sum(corpus_lens)/len(corpus_lens):.1f}")

    queries = random.Random(42).sample(TEST_QUERIES, min(n, len(TEST_QUERIES)))
    variants = [
        ("V1 全裸", dict(with_prompt=False, rag="none", check=False)),
        ("V2 仅prompt", dict(with_prompt=True, rag="none", check=False)),
        ("V3 prompt+RAG", dict(with_prompt=True, rag="relevant", check=False)),
        ("V4 全组件(当前)", dict(with_prompt=True, rag="relevant", check=True)),
        ("V5 RAG换随机", dict(with_prompt=True, rag="random", check=True)),
        ("V6 无RAG有校验", dict(with_prompt=True, rag="none", check=True)),
    ]
    results = {}
    for name, cfg in variants:
        bot = build_variant(name, corpus, **cfg)
        replies, costs = [], 0.0
        for q in queries:
            try:
                r = bot.chat(q, conv_id=f"abl_{name}")
                replies.append(r["reply"])
                costs += r["cost"]
            except Exception as e:  # noqa: BLE001
                print(f"  [!] {name} 失败: {e}")
                break
        if len(replies) == len(queries):
            results[name] = style_metrics(replies, corpus_lens,
                                          corpus_emoji_rate, corpus_verbal_rate)
            results[name]["cost"] = round(costs, 1)
            m = results[name]
            print(f"\n{name}: n={m['n']} 平均长={m['avgLen']} 长度W1={m['lenW1']} "
                  f"表情率={m['emojiRate']}(Δ{m['emojiRateDelta']:+.2f}) "
                  f"AI腔={m['aiClicheRate']} 口头禅={m['verbalRate']}(Δ{m['verbalDelta']:+.2f}) "
                  f"应答={m['ackRate']} 多样={m['uniqueRate']} {m['cost']}s")
        else:
            print(f"\n{name}: 未完成（{len(replies)}/{len(queries)}）")

    if results:
        print("\n=== 汇总（越接近语料基线越好；AI腔应=0；多样性别塌缩） ===")
        print(f"{'变体':<18}{'长度W1':>8}{'表情Δ':>8}{'AI腔':>7}{'口头禅Δ':>8}{'多样':>7}")
        for name, m in results.items():
            print(f"{name:<18}{m['lenW1']:>8.3f}{m['emojiRateDelta']:>+8.3f}"
                  f"{m['aiClicheRate']:>7.3f}{m['verbalDelta']:>+8.3f}{m['uniqueRate']:>7.3f}")
        out = ROOT / "ablation_e2e_result.json"
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细已存 {out}")


if __name__ == "__main__":
    main()
