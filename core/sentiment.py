# -*- coding: utf-8 -*-
"""
墨溯 core —— 中文情感倾向打分（纯离线、零第三方依赖）

设计原则：
  1. 不调用任何外部 LLM / API，开源用户离线可用
  2. 词典 + 否定/程度规则，可解释（命中哪些词都能说出来）
  3. 情感词用子串匹配：不依赖分词器，微信口语短句也稳

对外：
    score_text(text)            -> (score, pos_hits, neg_hits)  score ∈ [-1, 1]
    score_texts(texts)          -> 汇总 dict
    sentiment_curve(pairs)      -> 按时间桶的均分序列（pairs = [(ts, text)])
"""
from __future__ import annotations

from collections import Counter

# ---------------------------------------------------------------- 词典
POS = set("""
哈哈 嘻嘻 嘿嘿 开心 高兴 快乐 欢喜 喜欢 喜爱 爱 感谢 谢谢 多谢 辛苦 好的 好吧 可以
不错 棒 很棒 厉害 优秀 优异 美好 幸福 温暖 感动 心动 放心 靠谱 加油 支持 点赞 赞成
漂亮 帅 可爱 么么 么么哒 亲亲 抱抱 想你 思念 珍惜 幸运 庆幸 期待 期望 满意 舒服 轻松
愉快 高兴 热闹 有趣 有意思 搞笑 幽默 惊喜 圆满 顺利 成功 成了 成长 进步 收获 努力
认真 用心 体贴 温柔 善良 友好 和睦 甜 甜蜜 安心 安稳 踏实 给力 强大 牛 牛逼 精彩
享受 治愈 放松 清爽 舒服 好吃 好喝 好看 好玩 同意 答应 愿意 乐意 省心 贴心 懂事
进步 领先 高效 稳定 流畅 清晰 方便 便捷 实惠 划算 值得 放心 期待 希望 梦想 目标
""".split())

NEG = set("""
难过 伤心 悲伤 不开心 不高兴 烦 烦躁 烦躁 生气 气死 愤怒 讨厌 恨 委屈 难受 痛苦
失望 绝望 无奈 焦虑 担心 害怕 恐惧 累 疲惫 疲倦 好累 好困 无聊 孤独 寂寞 尴尬
郁闷 糟 糟糕 麻烦 恶心 垃圾 傻 蠢 笨 有病 烦人 不想 算了 无所谓 随便 敷衍 冷漠
冷淡 疏远 矛盾 吵架 争吵 冲突 误会 误解 对不起 抱歉 道歉 遗憾 后悔 愧疚 内疚
压力 崩溃 哭 眼泪 伤感 痛 疼 病 感冒 发烧 挂科 失败 输 穷 缺钱 加班 忙 熬夜
倒霉 惨 凄凉 冷 沉重 沉重 压抑 窒息 心累 心塞 崩 裂开 毁灭 不想活 讨厌 嫌弃
烦死 气人 过分 坑 坑人 骗 骗子 假 虚伪 狡猾 自私 无聊 腻 烦闷 难受 想哭 泪目
""".split())

NEGATION = set("不 没 别 无 非 甭 毫无 未必 不太 不怎么")
DEGREE = {"很": 1.5, "非常": 2.0, "太": 1.8, "超": 1.6, "特别": 1.6, "好": 1.3,
          "贼": 1.6, "真心": 1.5, "真的": 1.3, "有点": 0.7, "稍微": 0.6, "略": 0.7}


def score_text(text: str) -> tuple[float, list[str], list[str]]:
    """对单条文本打情感分。

    返回 (score, pos_hits, neg_hits)：
      score = (pos - neg) / (pos + neg)，空文本或无命中返回 0
    规则：情感词前 4 字内出现否定词 → 极性反转并衰减到 0.6；
          前 3 字内出现程度词 → 权重乘对应倍数。
    """
    if not text:
        return 0.0, [], []
    pos_w = neg_w = 0.0
    pos_hits: list[str] = []
    neg_hits: list[str] = []
    n = len(text)
    for w in POS:
        start = 0
        while True:
            i = text.find(w, start)
            if i < 0:
                break
            start = i + 1
            weight = 1.0
            window = text[max(0, i - 4):i]
            if any(x in window for x in NEGATION):
                weight = -0.6
            for d, k in DEGREE.items():
                if d in text[max(0, i - 3):i]:
                    weight *= k
                    break
            pos_w += weight
            pos_hits.append(w)
    for w in NEG:
        start = 0
        while True:
            i = text.find(w, start)
            if i < 0:
                break
            start = i + 1
            weight = 1.0
            window = text[max(0, i - 4):i]
            if any(x in window for x in NEGATION):
                weight = -0.6
            for d, k in DEGREE.items():
                if d in text[max(0, i - 3):i]:
                    weight *= k
                    break
            neg_w += weight
            neg_hits.append(w)
    total = abs(pos_w) + abs(neg_w)
    if total < 1e-9:
        return 0.0, [], []
    return max(-1.0, min(1.0, (pos_w - neg_w) / total)), pos_hits, neg_hits


def score_texts(texts) -> dict:
    """批量统计。threshold 之外的记中性。"""
    pos = neg = neu = 0
    total = 0.0
    top_pos: Counter = Counter()
    top_neg: Counter = Counter()
    for t in texts:
        if not t:
            continue
        s, ph, nh = score_text(t)
        total += s
        for w in ph:
            top_pos[w] += 1
        for w in nh:
            top_neg[w] += 1
        if s > 0.15:
            pos += 1
        elif s < -0.15:
            neg += 1
        else:
            neu += 1
    counted = pos + neg + neu
    return {
        "avg": round(total / counted, 3) if counted else 0.0,
        "positive": pos, "negative": neg, "neutral": neu,
        "positiveRate": round(pos / counted, 3) if counted else 0.0,
        "topPositiveWords": [{"word": w, "count": c} for w, c in top_pos.most_common(10)],
        "topNegativeWords": [{"word": w, "count": c} for w, c in top_neg.most_common(10)],
    }


def sentiment_curve(pairs, bucket: str = "month") -> list[dict]:
    """pairs = [(create_time, text)]，按月/周聚合均分。"""
    import time as _t
    fmt = {"month": "%Y-%m", "week": "%Y-%W", "day": "%Y-%m-%d"}.get(bucket, "%Y-%m")
    sums: dict[str, list[float]] = {}
    for ts, text in pairs:
        if not ts or not text:
            continue
        s, _, _ = score_text(text)
        k = _t.strftime(fmt, _t.localtime(ts))
        sums.setdefault(k, []).append(s)
    out = []
    for k in sorted(sums):
        v = sums[k]
        out.append({"bucket": k, "avg": round(sum(v) / len(v), 3), "count": len(v)})
    return out
