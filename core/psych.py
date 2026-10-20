# -*- coding: utf-8 -*-
"""心理学维度分析（基于聊天行为证据）。

## 为什么单独一个模块

`profile.py` 做的是**统计**（几点了还在聊、词频、回复间隔）。那些是事实，
但不是"这个人怎么样"。要从行为数据推到心理特征，中间需要一层可审计的推理：
每条判断都必须挂着**证据**和**置信度**，否则就成了算命。

本模块只做一条纪律：**不给结论性标签，只给「行为证据 → 可能倾向 → 置信度」**。
比如不说「她焦虑型依恋」，而说「已读不回后 3 小时内你连发 4 条，12 次里出现 9 次；
焦虑倾向证据强，但样本只覆盖 2 个月，可能是工作忙」。

## 维度来源

行为指标对齐心理学里靠行为可观测的框架，不用星座、MBTI 这类无行为锚点的东西：
- 依恋倾向（Hazan & Shaver / Brennan 的 ECR 行为面）：
  焦虑面看「对方延迟→我方追发」，回避面看「亲密话题后的退缩」
- 爱的语言（Chapman 五类）：靠消息里主动索取/给出的**类型偏好**推断
- 情绪调节：爆发式（短时高频+强负向）vs 压抑式（负向词少但间隔拉长）
- 沟通主动性：发起权分布、话题控制权
- 边界感：话题回避、拒绝性措辞

## 刻意不做的事

- 不做人格障碍判断、不做诊断性标签
- 不从单条消息推结论（最少要 3 次以上重复出现才算证据）
- 不出「她一定是什么人」这种句子
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 证据最少出现次数：低于这个数不下判断（避免单条消息定人格）
MIN_EVIDENCE = 3

# ---------------------------------------------------------------- 词表
# 说明：这些词表只用来**计数**，不做语义理解。
# 每个词表都必须能在中文口语里稳定命中，宁可少不可滥——
# 词表太宽会让"证据"变成噪音，反而降低可信度。

_ZHENG = ("嗯", "哦", "好的", "好", "行", "可以", "收到", "懂了", "明白", "ok", "okk")
_NEG_ACK = ("随便", "都行", "无所谓", "你定", "看你", "算了", "没事")
_REFUSE = ("不用了", "不要", "不去", "算了", "改天", "下次吧", "没空", "忙")
_CARE_GIVE = ("记得", "注意", "早点睡", "别熬夜", "多喝水", "吃饭", "小心", "照顾好")
_PRAISE = ("厉害", "好看", "漂亮", "可爱", "棒", "优秀", "真好", "喜欢你")
_SOOTHE = ("没事", "别怕", "我在", "会好的", "辛苦了", "抱抱", "慢慢来")
_CONFLICT = ("为什么", "你怎么", "凭什么", "到底", "是不是不想", "你到底")
_EVADE = ("再说吧", "以后再说", "不想聊这个", "别问了", "跳过", "换个话题")


def _count_any(texts, words) -> int:
    """有多少条消息命中了词表（按条计，不按次数——一条刷 10 个"嗯"只算 1 次）。"""
    n = 0
    for t in texts:
        if not t:
            continue
        low = t.lower()
        if any(w in low for w in words):
            n += 1
    return n


def _rate(n: int, total: int) -> float:
    return round(n / max(1, total), 3)


@dataclass
class Evidence:
    """一条证据：说的是「观察到什么」，不是「她是什么人」。"""
    dimension: str          # 维度 key
    behavior: str           # 观察到的行为（可计数的事实）
    value: float            # 数值（比例/次数）
    basis: int              # 样本量（分母）
    weight: str = "medium"  # high / medium / low
    caveat: str = ""        # 反例或替代解释

    def as_dict(self) -> dict:
        return {"dimension": self.dimension, "behavior": self.behavior,
                "value": self.value, "basis": self.basis,
                "weight": self.weight, "caveat": self.caveat}


@dataclass
class Dimension:
    """一个维度：证据汇总 + 倾向判断 + 置信度 + 反证。"""
    key: str
    label: str
    evidence: list[Evidence] = field(default_factory=list)

    def as_dict(self) -> dict:
        ev = [e.as_dict() for e in self.evidence]
        strong = [e for e in self.evidence if e.weight == "high"]
        total_basis = max([e.basis for e in self.evidence], default=0)
        # 置信度只看「证据强度 × 样本量」，不看好不好听
        if not ev:
            conf = "insufficient"
        elif total_basis < MIN_EVIDENCE:
            conf = "insufficient"
        elif len(strong) >= 2:
            conf = "high"
        elif len(strong) >= 1 or len(ev) >= 2:
            conf = "medium"
        else:
            conf = "low"
        return {"key": self.key, "label": self.label,
                "confidence": conf, "confidenceBasis": total_basis,
                "evidence": ev,
                "caveats": [e.caveat for e in self.evidence if e.caveat]}


class PsychAnalyzer:
    """把 contact_profile 已经算好的行为数据，翻成心理学维度。

    刻意**不吃原始消息**：profile 已经把 msgs 归约成统计量了，
    这里再重算一遍会得到两套口径（改一处忘一处就会不一致）。
    输入统一是 profile 的 dict，输出是附加维度。
    """

    def analyze(self, prof: dict, texts_mine=None, texts_theirs=None) -> dict:
        """prof：contact_profile / self_profile 的输出。
        texts_*：可选，双方的**原文列表**。仅用于「爱的语言」和「话题边界」
        这类必须看原句的维度——统计量里没有这些信息，硬从关键词猜会得出
        与事实无关的结论（第一版就是拿词频去匹配词表，全是噪音）。
        """
        inter = prof.get("interaction") or {}
        sent = prof.get("sentiment") or {}
        sig = prof.get("signals") or {}
        rhythm = prof.get("rhythm") or {}
        over = prof.get("overview") or {}
        tm = list(texts_mine or [])
        tt = list(texts_theirs or [])

        dims = [
            self._attachment_anxiety(inter, sig, over),
            self._attachment_avoidance(inter, sig, over),
            self._love_language(tm, tt),
            self._emotion_regulation(sent, rhythm),
            self._initiative(inter, over),
            self._boundary(tt),
            self._engagement(tt),
        ]
        dims = [d for d in dims if d is not None]

        return {
            "dimensions": [d.as_dict() for d in dims],
            "method": {
                "minEvidence": MIN_EVIDENCE,
                "note": "每条判断都带证据与置信度；证据不足时标 insufficient，不下结论。",
            },
            "disclaimer": (
                "以下是从聊天行为反推的倾向，不是心理诊断，也不能替代当面了解。"
                "样本只覆盖本会话的消息，双方在其他场合的样子不在此列。"
            ),
        }

    # ---------------------------------------------------------------- 依恋·焦虑面
    def _attachment_anxiety(self, inter, sig, over) -> Dimension:
        d = Dimension("attachment_anxiety", "依恋倾向·焦虑面")
        mine = over.get("myMessages") or 0
        theirs = over.get("theirMessages") or 0
        if mine + theirs < MIN_EVIDENCE:
            return d

        # 证据 1：我方发起比例（焦虑面的人更常主动开启对话）
        ini_me = inter.get("initiatedByMe") or 0
        ini_them = inter.get("initiatedByThem") or 0
        total_ini = ini_me + ini_them
        if total_ini:
            share = _rate(ini_me, total_ini)
            d.evidence.append(Evidence(
                "attachment_anxiety", "我方主动开启对话的比例", share, total_ini,
                weight="high" if share >= 0.6 else "medium",
                caveat="发起权也可能反映作息差，不一定是不安" if share < 0.6 else "",
            ))

        # 证据 2：回复延迟差（我方明显更快 = 更在意对方何时回）
        mr, tr = inter.get("myAvgReplySeconds"), inter.get("theirAvgReplySeconds")
        if mr and tr:
            ratio = round(tr / max(1, mr), 2)
            d.evidence.append(Evidence(
                "attachment_anxiety", "对方平均回复耗时 / 我方平均回复耗时", ratio,
                max(1, (mine + theirs) // 10), weight="high" if ratio >= 2 else "medium",
                caveat="也可能只是对方工作时段不看手机",
            ))
        return d

    # ---------------------------------------------------------------- 依恋·回避面
    def _attachment_avoidance(self, inter, sig, over) -> Dimension:
        d = Dimension("attachment_avoidance", "依恋倾向·回避面")
        mine = over.get("myMessages") or 0
        if mine < MIN_EVIDENCE:
            return d
        mr, tr = inter.get("myAvgReplySeconds"), inter.get("theirAvgReplySeconds")
        ini_me = inter.get("initiatedByMe") or 0
        ini_them = inter.get("initiatedByThem") or 0
        total_ini = ini_me + ini_them
        if not (mr and tr and total_ini):
            return d
        # 拆成两条独立证据，不要"两项同时成立才算一次"——
        # 那样永远凑不满 MIN_EVIDENCE，维度会永久停在"证据不足"（第一版就是这样）。
        slow = round(mr / max(1, tr), 2)
        if slow >= 1.3:
            d.evidence.append(Evidence(
                "attachment_avoidance", "我方平均回复耗时 / 对方平均回复耗时", slow,
                total_ini, weight="high" if slow >= 2 else "medium",
                caveat="回复慢更可能是在忙；只有「持续慢 + 很少主动」两者都指向回避",
            ))
        init_share = _rate(ini_me, total_ini)
        if init_share <= 0.4:
            d.evidence.append(Evidence(
                "attachment_avoidance", "我方主动开启对话的比例（越低越像被动方）",
                init_share, total_ini, weight="medium",
                caveat="也可能是对方本来就爱张罗，不代表我回避",
            ))
        return d

    # ---------------------------------------------------------------- 回应投入度
    def _engagement(self, texts_theirs) -> Dimension:
        """回应投入度：短促应答 vs 展开表达。

        这是最容易被误读的一维——「嗯」「哦」在有些关系里是冷淡，
        在另一些关系里只是对方的说话习惯。所以只报比例 + 明确写反例。
        """
        d = Dimension("engagement", "回应投入度（对方）")
        texts = [t for t in (texts_theirs or []) if t]
        n = len(texts)
        if n < MIN_EVIDENCE:
            return d
        short = _count_any(texts, _ZHENG)
        vague = _count_any(texts, _NEG_ACK)
        if short:
            d.evidence.append(Evidence(
                "engagement", "对方消息是短促应答（嗯/哦/好）的条数占比",
                _rate(short, n), n,
                weight="high" if _rate(short, n) >= 0.3 else "medium",
                caveat="短回答可能只是习惯或正在忙，不代表不在意；"
                       "要看是否只在特定话题上变短",
            ))
        if vague:
            d.evidence.append(Evidence(
                "engagement", "对方消息是「随便/都行/你定」类不表态的条数占比",
                _rate(vague, n), n, weight="medium",
                caveat="也可能是真的没偏好，或是把决定权交给你",
            ))
        return d

    # ---------------------------------------------------------------- 爱的语言
    def _love_language(self, texts_mine, texts_theirs) -> Dimension:
        """按「我方发出的消息里哪类表达最多」推断表达偏好。

        输入必须是**原句**：统计量（词频表）里没有「想你」「早点睡」这类
        短句，拿词频去匹配词表会得到一堆无关结果。
        """
        d = Dimension("love_language", "爱的语言倾向（我方表达）")
        texts = [t for t in (texts_mine or []) if t]
        n = len(texts)
        if n < MIN_EVIDENCE:
            return d
        buckets = {
            # 只放「明确表达好感」的，不放「在吗」「在干嘛」这类试探性开场——
            # 那是探对方在不在，不是表达好感，混进来会让"肯定的话语"虚高。
            "肯定的话语": _PRAISE,
            "服务的行动": _CARE_GIVE + _SOOTHE,
            "共享时光": ("见面", "一起", "出来", "约", "陪你", "等我", "带你"),
        }
        counts = {name: _count_any(texts, words) for name, words in buckets.items()}
        total = sum(counts.values())
        if not total:
            return d
        top = max(counts.items(), key=lambda kv: kv[1])
        # 每类给不同的反例：三条证据挂同一句 caveat 会在页面上重复三遍，
        # 看着像凑数，也让人以为那一句是针对这一条的
        caveats = {
            "肯定的话语": "夸人不等于关系近——有些人对外人更客气",
            "服务的行动": "叮嘱得多也可能是控制欲，要看对方是否真的接受",
            "共享时光": "提见面多不代表见得多，可能只是客套",
        }
        for name, c in sorted(counts.items(), key=lambda kv: -kv[1]):
            if not c:
                continue
            d.evidence.append(Evidence(
                "love_language", f"我方消息里属于「{name}」类的占比", _rate(c, total),
                n, weight="high" if name == top[0] and c >= MIN_EVIDENCE else "medium",
                caveat=caveats.get(name, ""),
            ))
        return d

    # ---------------------------------------------------------------- 情绪调节
    def _emotion_regulation(self, sent, rhythm) -> Dimension:
        d = Dimension("emotion_regulation", "情绪表达与调节")
        # 取对方（被分析对象）的情绪分布；没有就退到我方
        s = sent.get("theirs") or sent.get("mine") or {}
        if not isinstance(s, dict):
            return d
        pos = int(s.get("positive") or 0)
        neg = int(s.get("negative") or 0)
        neu = int(s.get("neutral") or 0)
        n = pos + neg + neu
        if n < MIN_EVIDENCE:
            return d
        neg_rate = _rate(neg, n)
        d.evidence.append(Evidence(
            "emotion_regulation", "负向情绪消息占比", neg_rate, n,
            weight="high" if neg_rate >= 0.2 else "medium",
            caveat="负向词也可能只是吐槽工作/天气，不等于情绪状态差",
        ))
        pos_rate = _rate(pos, n)
        d.evidence.append(Evidence(
            "emotion_regulation", "正向情绪消息占比", pos_rate, n,
            weight="medium",
            caveat="表情包和「哈哈」也算正向，可能高估情绪亮度",
        ))
        return d

    # ---------------------------------------------------------------- 主动性
    def _initiative(self, inter, over) -> Dimension:
        d = Dimension("initiative", "沟通主动性")
        ini_me = inter.get("initiatedByMe") or 0
        ini_them = inter.get("initiatedByThem") or 0
        total = ini_me + ini_them
        if total < MIN_EVIDENCE:
            return d
        share = _rate(ini_them, total)
        d.evidence.append(Evidence(
            "initiative", "对话由对方开启的比例", share, total,
            weight="high" if share >= 0.5 else "medium",
            caveat="间隔 6h 以上才算新对话，短时多轮被合并，可能低估对方主动性",
        ))
        return d

    # ---------------------------------------------------------------- 边界
    def _boundary(self, texts_theirs) -> Dimension:
        """话题边界：从对方的原句里数「拒绝/回避」类措辞。"""
        d = Dimension("boundary", "话题边界（对方）")
        texts = [t for t in (texts_theirs or []) if t]
        n = len(texts)
        if n < MIN_EVIDENCE:
            return d
        for label, words in (("拒绝性措辞", _REFUSE), ("话题回避措辞", _EVADE),
                             ("冲突性质问", _CONFLICT)):
            c = _count_any(texts, words)
            if c < MIN_EVIDENCE:
                continue
            d.evidence.append(Evidence(
                "boundary", f"对方消息含「{label}」的占比", _rate(c, n), n,
                weight="medium",
                caveat={
                    "拒绝性措辞": "「改天」「下次吧」在中文里常是客套，未必真拒绝",
                    "话题回避措辞": "「再说吧」也可能只是真的忙，想看是否集中在某类话题",
                    "冲突性质问": "「为什么」大量出现在解释和讲故事里，不等于在质问",
                }.get(label, "关键词拿不到语气，只能当线索"),
            ))
        return d


def _fmt_ev(e: dict) -> str:
    """按证据类型决定显示格式。

    不能只看「值是否 ≤1」——「回复耗时比 1.47」是倍数不是比例，
    按比例显示会变成 147%，直接读错。
    """
    beh = e.get("behavior") or ""
    v = e.get("value")
    # 描述里带「比」「耗时」的是倍数关系，原样显示
    if ("之比" in beh) or ("耗时" in beh and "/" in beh) or ("倍" in beh):
        return f"{v:g}" if isinstance(v, float) else str(v)
    if isinstance(v, float) and 0 <= v <= 1:
        # 用原始值判断阈值，不能先 round 再比：0.0006*100=0.06 会被
        # round 成 0.1，正好卡在阈值上，把「0.06%」显示成「0.1%」（夸大十倍）
        if v * 100 < 0.1:
            return "<0.1%"
        return f"{round(v * 100, 1)}%"
    return f"{v:g}" if isinstance(v, float) else str(v)


def render_psych(data: dict) -> str:
    """把心理学维度渲染成 markdown（CLI 用）。"""
    dims = data.get("dimensions") or []
    if not dims:
        return "（样本不足，未生成心理学维度）\n"
    conf_cn = {"high": "强", "medium": "中", "low": "弱", "insufficient": "证据不足"}
    out = []
    for d in dims:
        out.append(f"### {d['label']}")
        out.append(f"置信度：**{conf_cn.get(d['confidence'], d['confidence'])}**"
                   f"（样本 {d['confidenceBasis']}）")
        for e in d["evidence"]:
            out.append(f"- {e['behavior']}：{_fmt_ev(e)}（样本 {e['basis']}）")
            if e["caveat"]:
                out.append(f"  - 反例可能：{e['caveat']}")
        out.append("")
    out.append(f"> {data.get('disclaimer', '')}")
    return "\n".join(out)
