# -*- coding: utf-8 -*-
"""AI 人格分析层 —— 把统计画像升级为「全面人格分析」。

## 为什么单独一个模块

`profile.py` 产出的是**统计**（几点在聊、词频、回复间隔），`psych.py` 是**规则
推断**（词表计数 → 倾向 + 置信度）。两者都到不了"这个人是个什么样的人"——
那需要语义级的综合，只有 LLM 能做。本模块补上这一层。

## 设计纪律（与 bot / psych 一脉相承）

1. **AI 是附加层，不是替代层**：统计与规则推断的结果原样保留，AI 章节单独挂
   `profile["ai"]`。没配大模型 → 自动降级，纯离线可用（项目铁律）。
2. **引文锚定（防编造的核心机制）**：prompt 里给的每条消息样本都带 uid，
   LLM 的每条证据必须引用 uid + 逐字原话。校验器逐条核对：uid 不存在、或
   quote 与该样本对不上（子串 / 字符级 Dice ≥ 0.6），该证据整条丢弃。
   维度丢了全部证据 → 标 insufficient，不留"看着有据其实无据"的判断。
3. **禁诊断性语言**：抑郁症/焦虑症/人格障碍/确诊……出现即整维丢弃。
   分析行为倾向可以，下医学诊断不行（psych.py 的同一条纪律）。
4. **置信度纪律**：证据 <2 条不得标 high，<1 条必须 insufficient。
5. **缓存**：LLM 调用要花钱，结果按「范围+会话+时间范围+模型+数据签名」
   缓存一小时；数据变了签名变，自动失效（复用 core/cache.py 的机制）。

## 隐私边界

self 画像只发**本人发言**；contact 画像会发**双方采样**（分析对方与关系
必须看两边）。这些消息会发送到你配置的 LLM 端点——介意就 `--no-ai`。
演示模式（MOSU_DEMO=1，截图脱敏）一律不调用 AI。
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime

# 环境总开关：MOSU_PROFILE_AI=0 时任何路径都不调用 LLM（CI / 成本控制）
ENV_KILL = "MOSU_PROFILE_AI"

# 医学/人格障碍诊断词：出现即整维丢弃。描述行为倾向不拦，下诊断不行。
# 用词干而非全词：「抑郁倾向」也要拦得住（全词「抑郁症」会漏）。
DIAGNOSTIC_TERMS = (
    "抑郁", "焦虑", "躁郁", "双相", "人格障碍", "自恋型", "边缘型",
    "精神病", "精神疾病", "强迫症", "创伤后", "心理疾病", "确诊", "患病",
    "依恋障碍", "社交恐惧", "多动症",
)

# 维度定义：key → (标签, 行为锚点说明)。contact 额外看关系动态。
_SELF_DIMS = [
    ("personality", "性格特质",
     "大五的行为面（开放性/尽责性/外向性/宜人意/情绪稳定性），只写聊天中可观察到的倾向"),
    ("emotion", "情绪模式",
     "情绪基线、波动、调节方式（爆发式/压抑式/转移式）"),
    ("social", "社交模式",
     "主动性、亲密距离、在不同人面前的分寸差、群体角色"),
    ("cognition", "认知与思维",
     "决策风格（果断/纠结）、话题深度、信息处理习惯"),
    ("expression", "表达风格",
     "语气、幽默感、礼貌结构、标点与表情使用习惯"),
    ("values", "价值观与关注点",
     "从反复出现的话题与原话里反复出现的主题，推断在意什么"),
]
_CONTACT_DIMS = _SELF_DIMS[:4] + [
    ("expression", "表达风格",
     "语气、幽默感、礼貌结构、标点与表情使用习惯"),
    ("relationship", "关系动态",
     "双方的互动模式：主动权在谁、亲密化还是疏远化的轨迹、各自的角色"),
]

# prompt/schema 版本号：改 prompt 必须 +1，否则缓存会返回旧提示词的结果
# （真机联调改过一次方向语义，靠这个版本号失效的缓存）
PROMPT_VERSION = 3

AI_CACHE = None          # 懒加载（避免 import 期构造缓存对象）


def _cache():
    global AI_CACHE
    if AI_CACHE is None:
        from core.cache import ResultCache
        AI_CACHE = ResultCache(maxsize=8, ttl=3600)
    return AI_CACHE


# ---------------------------------------------------------------- 采样
def _stratify(msgs: list[dict], limit: int) -> list[dict]:
    """分层抽样：按会话轮询取条，避免单个大群挤占全部样本。

    只有一个会话（contact 画像的场景）时退化为时间均匀采样——
    关系是动态的，只看最近一段会丢掉演变轨迹。
    """
    if len(msgs) <= limit:
        return list(msgs)
    by_sess: dict[str, list[dict]] = defaultdict(list)
    for m in msgs:
        by_sess[m.get("session") or "_"].append(m)
    if len(by_sess) <= 1:
        step = len(msgs) / limit
        return [msgs[int(i * step)] for i in range(limit)]
    out: list[dict] = []
    i = 0
    while len(out) < limit:
        progressed = False
        for k in sorted(by_sess, key=lambda k: -len(by_sess[k])):
            lst = by_sess[k]
            if i < len(lst):
                out.append(lst[i])
                progressed = True
            if len(out) >= limit:
                break
        if not progressed:
            break
        i += 1
    return out


def _fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "--"


def pick_samples(msgs: list[dict], scope: str, per_side: int = 90) -> list[dict]:
    """从 profile 收集到的消息里挑 LLM 样本，统一打 uid。

    uid 是引文锚定的锚：prompt 里每条样本带 uid，LLM 引文必须报 uid，
    校验器按 uid 核对原话——比纯文本匹配可靠得多（重名、相似句不误判）。
    """
    mine = [m for m in (msgs or []) if m.get("direction") == "out" and m.get("text")]
    theirs = [m for m in (msgs or []) if m.get("direction") != "out" and m.get("text")]
    out: list[dict] = []
    if scope == "contact":
        a, b = _stratify(mine, per_side), _stratify(theirs, per_side)
        for i, m in enumerate(a):
            m["_uid"] = f"m{i}"
        for i, m in enumerate(b):
            m["_uid"] = f"t{i}"
        out = sorted(a + b, key=lambda m: m.get("ts") or 0)
    else:
        a = _stratify(mine, per_side * 2)
        for i, m in enumerate(a):
            m["_uid"] = f"m{i}"
        out = a
    return [{"uid": m["_uid"], "ts": _fmt_ts(m.get("ts")),
             "dir": "我" if m.get("direction") == "out" else "对方",
             "text": (m.get("text") or "")[:80]} for m in out]


# ---------------------------------------------------------------- 统计压缩
def _compact(profile: dict, scope: str) -> list[str]:
    """把统计结果压成可引用的短事实行（学 facts.py：数字已核对，禁二次推理）。"""
    o = profile.get("overview") or {}
    L: list[str] = []
    if scope == "self":
        r = profile.get("rhythm") or {}
        L.append(f"总量 {o.get('totalMessages')} 条，本人 {o.get('myMessages')} 条"
                 f"（{o.get('myShare', 0):.0%}），跨度 {o.get('spanDays')} 天，"
                 f"活跃 {o.get('activeDays')} 天")
        if r.get("peakHour") is not None:
            L.append(f"发言高峰 {r['peakHour']:02d} 点，最活跃 {r.get('peakWeekday')}，"
                     f"深夜 0-6 点 {r.get('nightMessages')} 条，"
                     f"早间 6-10 点 {r.get('morningMessages')} 条")
        for s in (profile.get("social") or [])[:3]:
            L.append(f"往来Top：{s['name']}（我 {s['mine']} / 总 {s['total']}）")
        tp = profile.get("topics") or {}
        if tp.get("byTopic"):
            L.append("话题Top5：" + "、".join(
                f"{t['topic']}({t['hits']})" for t in tp["byTopic"][:5]))
        if tp.get("keywords"):
            L.append("高频词Top10：" + "、".join(
                f"{k['word']}({k['count']})" for k in tp["keywords"][:10]))
        st = profile.get("style") or {}
        if st:
            L.append(f"风格：平均 {st.get('avgLength')} 字，表情率 "
                     f"{st.get('emojiRate', 0):.1%}，提问率 {st.get('questionRate', 0):.1%}")
    else:
        it, sg, sig = (profile.get("interaction") or {},
                       profile.get("sentiment") or {}, profile.get("signals") or {})
        L.append(f"往来 {o.get('totalMessages')} 条：我 {o.get('myMessages')} / "
                 f"对方 {o.get('theirMessages')}（我占 {o.get('myShare', 0):.0%}），"
                 f"跨度 {o.get('spanDays')} 天，活跃 {o.get('activeDays')} 天")
        L.append(f"发起：我 {it.get('initiatedByMe')} 次 / 对方 "
                 f"{it.get('initiatedByThem')} 次；平均回复：我 "
                 f"{it.get('myAvgReplySeconds')}s / 对方 {it.get('theirAvgReplySeconds')}s")
        m, t = sg.get("mine") or {}, sg.get("theirs") or {}
        if m or t:
            L.append(f"情感均分（词典 -1~1）：我 {m.get('avg', 0):+.2f} / "
                     f"对方 {t.get('avg', 0):+.2f}")
        tp = profile.get("topics") or {}
        if tp.get("mine"):
            L.append("我方话题Top3：" + "、".join(
                x["topic"] for x in tp["mine"][:3]))
        if tp.get("theirs"):
            L.append("对方话题Top3：" + "、".join(
                x["topic"] for x in tp["theirs"][:3]))
        if sig:
            L.append(f"亲密度信号：表情率 {sig.get('emojiRate', 0):.1%}，"
                     f"笑声每百条 {sig.get('laughPer100')} 个「哈」，"
                     f"长度趋势 {sig.get('lengthTrend')}，媒体占比 "
                     f"{sig.get('mediaShare', 0):.1%}")
    psych = (profile.get("psych") or {}).get("dimensions") or []
    if psych:
        conf = {"high": "强", "medium": "中", "low": "弱", "insufficient": "不足"}
        parts = []
        for d in psych:
            ev = d.get("evidence") or []
            top = ev[0] if ev else None
            parts.append(f"{d.get('label')}[{conf.get(d.get('confidence'))}]"
                         + (f"：{top.get('behavior')}={top.get('value')}" if top else ""))
        L.append("规则引擎已发现的倾向（供参照，你要独立判断）：" + "；".join(parts))
    return L


# ---------------------------------------------------------------- prompt
_SYSTEM = """你是一位基于聊天行为证据的人格分析师。输入只有两部分：本机统计出的客观事实，以及带编号的真实消息样本。纪律（违反任何一条，输出整体作废）：

1. 只基于给定材料下判断，不得引入外部猜测。材料不足以覆盖的维度，confidence 标 "insufficient" 并说明缺什么。
2. 每条论断必须挂 evidence：引用样本编号（uid）加原话片段（quote）。quote 必须逐字取自该编号的样本，不得改写、拼接、翻译。没有引文的论断一律删除。
3. 每个维度必须给 counterpoints：至少一条替代解释或反例（样本偏差、习惯、情境因素）。
4. 置信度纪律：证据少于 2 条不得标 high；少于 1 条必须标 insufficient。
5. 禁止诊断性语言：不得出现抑郁症、焦虑症、人格障碍、确诊、患病等医学标签。只描述行为倾向。
6. 只输出一个 JSON 对象本身：不要输出 markdown 代码块标记，不要任何额外文字。
7. 不要输出思考过程、推理、草稿或自我修正——你的第一个字符必须是 {，最后一个字符必须是 }。"""


def _dims_for(scope: str) -> list[tuple[str, str, str]]:
    return _CONTACT_DIMS if scope == "contact" else _SELF_DIMS


# 这些维度描述的是「对方是什么人」，证据必须取自对方方向的消息。
# 真机联调抓到过：模型把本人的短回复（「没有」「6个人」）当成对方的
# 说话风格，整个判断张冠李戴——单句提醒靠不住，用校验器硬拦。
_DIMS_NEED_THEIRS = {"personality", "emotion", "cognition", "expression"}


def build_messages(profile: dict, samples: list[dict], scope: str,
                   subject: str = "", self_name: str = "") -> list[dict]:
    dims = _dims_for(scope)
    facts = "\n".join(f"- {x}" for x in _compact(profile, scope)) or "- （无统计）"
    # 方向用**实名**标注，不用「我/对方」：实测推理模型会把「我」误读成
    # 分析对象（整个分析都在说对方，模型顺势把样本里的「我」当成对方），
    # 于是本人的短回复被安成对方的说话风格。实名消除这个歧义。
    me = self_name or "本人"
    you = subject or "对方"
    blocks = "\n".join(
        f"{s['uid']} [{s['ts']} {me if s['dir'] == '我' else you}] {s['text']}"
        for s in samples)
    dim_lines = "\n".join(f"- {k}｜{label}｜{anchor}" for k, label, anchor in dims)
    schema = ('{"summary": "总体侧写，150-250字，说人话，不堆术语", '
              '"dimensions": [{"key": "维度key", "label": "维度名", '
              '"assessment": "判断（说行为倾向，不说诊断）", '
              '"confidence": "high 或 medium 或 low 或 insufficient", '
              '"evidence": [{"uid": "样本编号", "quote": "逐字原话", '
              '"analysis": "这条证据说明什么"}], '
              '"counterpoints": ["替代解释或反例"]}]}')
    who = subject or ("本人（全部发言）" if scope == "self" else "对方与这段关系（双方发言）")
    direction_note = ""
    if scope == "contact":
        direction_note = (
            f"\n\n【方向语义——搞反会全盘皆错】"
            f"样本里标「{me}」的消息是账号所有者（数据主人）发的；"
            f"标「{you}」的消息才是分析对象。"
            f"描述{you}的性格/情绪/思维/表达风格的引文，必须取自{you}方向的消息；"
            f"取自{me}方向的消息只能用于描述{me}本人或双方的互动模式。")
    user = (f"【分析对象】{who}\n\n"
            f"【统计事实】（数字已核对，可直接引用，不要自己另算）\n{facts}\n\n"
            f"【消息样本】（uid [日期 时间 发送者] 内容）\n{blocks}{direction_note}\n\n"
            f"【必须覆盖的维度】（key｜名称｜行为锚点）\n{dim_lines}\n\n"
            f"【输出格式】只输出 JSON：\n{schema}")
    return [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user}]


# ---------------------------------------------------------------- 解析与校验
def _parse_json(raw: str):
    """从 LLM 回复里提取 JSON。

    推理模型（如 deepseek-v4-flash）会把思考过程吐进 content，JSON 埋在
    独白后面；还有的会裹 ```json 围栏。策略：整体解析 → 围栏剥离 →
    扫描所有花括号配对的对象，**从后往前**试（最终答案通常在最后），
    第一个能解析且含 dimensions/summary 的算数。
    """
    if not raw:
        return None
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt)
    try:
        obj = json.loads(txt)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    candidates: list[str] = []
    depth, start = 0, None
    for i, ch in enumerate(txt):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(txt[start:i + 1])
                start = None
    for cand in reversed(candidates):
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict) and ("dimensions" in obj or "summary" in obj):
            return obj
    return None


def _dice(a: str, b: str) -> float:
    """字符级 Dice 系数（短中文文本够用，与 checker.py 同一思路）。"""
    if not a or not b:
        return 0.0
    ba = {a[i:i + 2] for i in range(max(1, len(a) - 1))} or {a}
    bb = {b[i:i + 2] for i in range(max(1, len(b) - 1))} or {b}
    if not bb:
        bb = {b}
    return 2 * len(ba & bb) / (len(ba) + len(bb))


def _match_sample(uid: str, quote: str, samples: list[dict]):
    """按 uid 找样本并核对原话；uid 失效时退回全局文本匹配。

    返回 (sample, note)：sample 为 None 表示引文不可信。
    """
    q = (quote or "").strip()
    if not q or len(q) > 80:
        return None, "引文为空或过长"
    by_uid = {s["uid"]: s for s in samples}
    s = by_uid.get(uid)
    if s is not None:
        if q in s["text"] or s["text"] in q:
            return s, ""
        if _dice(q, s["text"]) >= 0.6:
            return s, "原话与样本略有出入（相似度通过）"
        return None, f"引文与样本 {uid} 对不上"
    for cand in samples:
        if q in cand["text"]:
            return cand, f"uid {uid} 不存在，但原话匹配到 {cand['uid']}"
    return None, f"样本 {uid} 不存在"


def _has_diagnostic(text: str) -> str:
    for w in DIAGNOSTIC_TERMS:
        if w in text:
            return w
    return ""


def validate(parsed: dict, samples: list[dict], scope: str) -> dict:
    """把 LLM 输出过一遍硬校验：引文锚定 + 诊断词 + 置信度纪律。"""
    allowed = {k: label for k, label, _ in _dims_for(scope)}
    dims_out, dropped = [], []
    for d in parsed.get("dimensions") or []:
        key = str(d.get("key") or "")
        label = str(d.get("label") or allowed.get(key) or key)
        assessment = str(d.get("assessment") or "")
        if key not in allowed:
            dropped.append({"label": label, "reason": f"未知维度 {key}"})
            continue
        bad = _has_diagnostic(assessment + label)
        if bad:
            dropped.append({"label": label, "reason": f"出现诊断性用语「{bad}」"})
            continue
        ev_out = []
        for e in d.get("evidence") or []:
            s, note = _match_sample(str(e.get("uid") or ""),
                                    str(e.get("quote") or ""), samples)
            if s is None:
                continue
            ev_out.append({"uid": s["uid"], "ts": s["ts"], "dir": s["dir"],
                           "quote": (e.get("quote") or "").strip(),
                           "analysis": str(e.get("analysis") or "")[:120],
                           "note": note})
        conf = str(d.get("confidence") or "low")
        if conf not in ("high", "medium", "low", "insufficient"):
            conf = "low"
        # 置信度纪律：不按 LLM 自称的来，按校验后剩下的证据数来
        if len(ev_out) >= 2:
            pass
        elif len(ev_out) == 1:
            conf = "medium" if conf == "high" else conf
        else:
            conf = "insufficient"
        # 方向硬规则：描述「对方是什么人」的维度，证据不能全取自本人消息
        # （真机联调实测过模型把本人短回复当成对方风格，整维张冠李戴）
        has_theirs_sample = any(s["uid"].startswith("t") for s in samples)
        if (scope == "contact" and key in _DIMS_NEED_THEIRS
                and has_theirs_sample and ev_out
                and all(e["uid"].startswith("m") for e in ev_out)):
            dropped.append({"label": label,
                            "reason": "描述对方的维度只引用了本人消息（方向标反）"})
            continue
        cps = [str(c)[:120] for c in (d.get("counterpoints") or [])][:4]
        dims_out.append({"key": key, "label": label, "assessment": assessment,
                         "confidence": conf, "evidence": ev_out,
                         "counterpoints": cps})
        if conf == "insufficient":
            dropped.append({"label": label, "reason": "没有通过校验的引文证据"})
    return {
        "summary": str(parsed.get("summary") or "")[:600],
        "dimensions": [d for d in dims_out if d["confidence"] != "insufficient"],
        "insufficient": [d["label"] for d in dims_out if d["confidence"] == "insufficient"],
        "dropped": dropped,
    }


# ---------------------------------------------------------------- 主类
class AiProfileAnalyzer:
    """给画像挂 AI 分析章节。LLM 可注入（测试用假客户端）。"""

    def __init__(self, ds=None, llm=None, cache: bool = True,
                 max_retries: int = 1):
        self.ds = ds
        self._llm = llm
        self.cache_on = cache
        self.max_retries = max_retries
        self._client = None

    # -------------------------------------------------- 客户端
    def _client_or_none(self):
        if self._llm is not None:
            return self._llm
        if self._client is not None:
            return self._client
        try:
            from core.bot.llm import LlmClient
            self._client = LlmClient(timeout=90)
        except (Exception, SystemExit):
            # 没配 key 时 LlmClient 抛的是 SystemExit（不是 Exception 子类），
            # 只捕 Exception 会让「未配置」直接崩掉调用方而不是优雅降级
            return None
        return self._client

    def available(self) -> bool:
        """没配大模型 → False，调用方走纯统计降级（不抛异常）。"""
        if os.environ.get(ENV_KILL) == "0":
            return False
        return self._client_or_none() is not None

    # -------------------------------------------------- 分析
    def analyze(self, profile: dict, msgs: list[dict], scope: str = "self",
                session_key: str = "", from_ts=None, to_ts=None,
                subject: str = "", self_name: str = "") -> dict:
        if not self.available():
            return {"available": False,
                    "reason": "未配置大模型（GUI 设置页填 URL/Key，或设 "
                              "MOSU_BOT_BASE_URL / MOSU_BOT_API_KEY / MOSU_BOT_MODEL）"}
        if getattr(self.ds, "demo", False):
            return {"available": False, "reason": "演示模式（截图脱敏）不调用 AI"}
        samples = pick_samples(msgs, scope)
        if len(samples) < 5:
            return {"available": False,
                    "reason": f"样本不足（{len(samples)} 条），不做 AI 分析"}

        client = self._client_or_none()
        model = getattr(client, "model", "?")
        sig = ""
        if self.ds is not None:
            try:
                from core.cache import dataset_signature
                sig = dataset_signature(self.ds)
            except Exception:
                sig = ""
        key = ("aiprofile", PROMPT_VERSION, scope, session_key,
               str(from_ts or ""), str(to_ts or ""), model, sig)
        if self.cache_on:
            hit = _cache().get(key)
            if hit is not None:
                out = dict(hit)
                out["cached"] = True
                return out

        t0 = time.time()
        messages = build_messages(profile, samples, scope, subject=subject,
                                  self_name=self_name)
        parsed, err = None, ""
        for attempt in range(self.max_retries + 1):
            try:
                raw = client.chat(messages, temperature=0.3, max_tokens=5000)
                parsed = _parse_json(raw)
                if parsed is not None:
                    break
                err = "LLM 未返回合法 JSON（可能混入了思考过程）"
            except Exception as e:               # noqa: BLE001
                parsed, err = None, f"{type(e).__name__}: {e}"
            if attempt < self.max_retries:
                messages = messages + [
                    {"role": "user", "content":
                     "上一条回复无法解析为 JSON（可能包含思考过程或多余文字）。"
                     "重新输出：第一个字符必须是 {，最后一个必须是 }，"
                     "只含 JSON 本身，不要任何解释。"}]
        if parsed is None:
            return {"available": False, "reason": f"AI 分析失败：{err}"}

        result = validate(parsed, samples, scope)
        result.update({
            "available": True,
            "scope": scope,
            "model": model,
            "sampleCount": len(samples),
            "cost": round(time.time() - t0, 1),
            "disclaimer": ("AI 人格分析基于采样消息由大模型推断，可能出错；"
                           "不是心理诊断，也不能替代本人判断。"
                           "联系人画像会将双方采样消息发送至你所配置的 LLM 端点。"),
        })
        if self.cache_on:
            _cache().set(key, result)
        return result


# ---------------------------------------------------------------- 渲染
def render_ai(ai: dict) -> str:
    """把 AI 分析渲染成 markdown（CLI 画像报告用）。"""
    conf_cn = {"high": "强", "medium": "中", "low": "弱", "insufficient": "证据不足"}
    L = []
    L.append(f"> 模型 {ai.get('model', '?')}｜样本 {ai.get('sampleCount', 0)} 条"
             f"｜耗时 {ai.get('cost', '?')}s"
             + ("｜（缓存结果）" if ai.get("cached") else ""))
    L.append("")
    if ai.get("summary"):
        L.append(f"**总体侧写**：{ai['summary']}")
        L.append("")
    for d in ai.get("dimensions") or []:
        L.append(f"### {d['label']}（置信度：{conf_cn.get(d['confidence'], d['confidence'])}）")
        L.append("")
        L.append(d.get("assessment") or "")
        L.append("")
        for e in d.get("evidence") or []:
            note = f"（{e['note']}）" if e.get("note") else ""
            L.append(f"- 「{e['quote']}」〔{e['uid']} {e['ts']} {e['dir']}〕{note}"
                     f"—— {e.get('analysis', '')}")
        for c in d.get("counterpoints") or []:
            L.append(f"  - 反例/替代解释：{c}")
        L.append("")
    ins = ai.get("insufficient") or []
    if ins:
        L.append(f"> 证据不足未下结论的维度：{'、'.join(ins)}")
        L.append("")
    dropped = ai.get("dropped") or []
    if dropped:
        reasons = "；".join(f"{x['label']}（{x['reason']}）" for x in dropped[:5])
        L.append(f"> 未通过证据校验被丢弃的 AI 输出：{reasons}")
        L.append("")
    L.append(f"> {ai.get('disclaimer', '')}")
    return "\n".join(L)
