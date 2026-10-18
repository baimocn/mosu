# -*- coding: utf-8 -*-
"""
墨溯 core —— 个人语料与风格画像（做"个人 bot"的地基）

从全库消息里抽取**本人发出**的内容，产出：

  1. corpus.jsonl          全量语料（每条含时间/会话/类型/原文/表情列表）
  2. corpus_by_session/    按会话分段的纯文本（我的发言时间线，含 [表情] 标记）
  3. style_profile.md      风格画像（LLM system prompt 草稿 + 人工可读报告）

表情处理（微信风格复现的关键）：
  - 文本里内嵌的 [旺柴] [捂脸] —— 保留原文，可统计、可复现
  - 独立表情消息（kind=emoji）—— 原文只有内部编号，标注 [表情] 并保留 md5
    （图片本体可用 mosu_media 从媒体库导出）
  - 图片/语音/视频 —— 占位标注，语音附转写文字
"""
from __future__ import annotations

import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from core.analytics import extract_keywords
from core.dataset import KIND_BY_TYPE
from core.profile import TOPIC_LEXICON

_EMOJI_RE = re.compile(r"\[([一-龥]{1,8})\]")
_XMLISH = re.compile(r"^<[a-zA-Z]")

# 语料占位符（不是真表情，统计时要排除）
PLACEHOLDERS = {"表情", "图片", "文件", "视频", "语音"}


def _clean_for_corpus(text: str, kind: str, m: dict) -> str:
    """语料用清洗：只清 XML 消息体和内部 id 串，**保留 [表情] 占位符**。"""
    t = (text or "").strip()
    if kind == "emoji":
        return "[表情]"
    if kind == "image":
        return "[图片]"
    if kind == "video":
        return "[视频]"
    if kind == "voice":
        ms = m.get("voiceMs") or 0
        vt = m.get("voiceText") or ""
        return f"[语音 {ms}秒]" + (f" {vt}" if vt else "")
    if kind == "file":
        return f"[文件] {m.get('title') or m.get('fileName') or ''}"
    if _XMLISH.search(t) and ">" in t:
        return ""
    if len(t) >= 8 and not any("一" <= c <= "龥" for c in t) and ("." in t or "_" in t):
        return ""          # com.tencent.xin... 之类内部 id
    return t


class PersonaBuilder:
    def __init__(self, ds):
        self.ds = ds

    # ------------------------------------------------------------ 语料收集
    def collect(self) -> list[dict]:
        """全库遍历本人发出的消息（约 1-2 分钟）。"""
        out: list[dict] = []
        t0 = time.time()
        sessions = sorted(self.ds.sessions(),
                          key=lambda s: s.get("sortTime") or s.get("lastTime") or 0, reverse=True)
        for si, sess in enumerate(sessions, 1):
            for shard, r in self.ds.iter_session_rows(sess):
                m = self.ds.to_message(r, sess, shard=shard)
                if m["direction"] != "out":
                    continue
                kind = m.get("kind") or "other"
                text = _clean_for_corpus(m.get("content"), kind, m)
                if not text:
                    continue
                out.append({
                    "ts": m.get("createTime"),
                    "time": datetime.fromtimestamp(m["createTime"]).strftime("%Y-%m-%d %H:%M")
                            if m.get("createTime") else "",
                    "session": sess["displayName"],
                    "sessionType": sess["type"],
                    "kind": kind,
                    "text": text,
                    "emojis": _EMOJI_RE.findall(text),
                    "imgMd5": m.get("imgMd5"),
                    "len": len(text),
                })
            if si % 30 == 0:
                print(f"  [{si}/{len(sessions)}] 已收集 {len(out)} 条…", flush=True)
        out.sort(key=lambda x: x["ts"] or 0)
        print(f"  [✓] 语料收集完成 {len(out)} 条（{time.time()-t0:.0f}s）", flush=True)
        return out

    # ------------------------------------------------------------ 落盘
    def write_corpus(self, corpus: list[dict], out_dir: Path) -> dict:
        out_dir.mkdir(parents=True, exist_ok=True)
        # 1) jsonl 全量
        import json
        with open(out_dir / "corpus.jsonl", "w", encoding="utf-8") as f:
            for c in corpus:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        # 2) 按会话分段
        by_sess: dict[str, list[dict]] = {}
        for c in corpus:
            by_sess.setdefault(c["session"], []).append(c)
        sess_dir = out_dir / "corpus_by_session"
        sess_dir.mkdir(parents=True, exist_ok=True)
        bad = '<>:"/\\|?*'
        for name, items in by_sess.items():
            safe = "".join(ch for ch in name if ch not in bad).strip() or "chat"
            lines = [f"# {name}（我的发言 {len(items)} 条）", ""]
            for c in items:
                lines.append(f"[{c['time']}] {c['text']}")
            (sess_dir / f"{safe}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"corpus": len(corpus), "sessions": len(by_sess),
                "jsonl": str(out_dir / "corpus.jsonl"),
                "bySessionDir": str(sess_dir)}

    # ------------------------------------------------------------ 风格画像
    def build_profile(self, corpus: list[dict]) -> dict:
        n = len(corpus)
        texts = [c["text"] for c in corpus]                 # 全部（长度/标点统计用）
        kw_texts = [c["text"] for c in corpus if c["kind"] == "text"]   # 词频只用纯文本
        lens = [c["len"] for c in corpus]
        short = sum(1 for x in lens if x <= 10)
        mid = sum(1 for x in lens if 10 < x <= 50)
        long_ = sum(1 for x in lens if x > 50)

        # 标点习惯
        q = sum(1 for t in texts if "?" in t or "？" in t)
        ex = sum(1 for t in texts if "!" in t or "！" in t)
        ell = sum(1 for t in texts if "…" in t or "..." in t)
        nopunct = sum(1 for t in texts if len(t) >= 4 and not re.search(r"[，。！？、~…,.!?]", t))

        # 表情
        emoji_counter: Counter = Counter()
        for c in corpus:
            emoji_counter.update(e for e in c["emojis"] if e not in PLACEHOLDERS)
        standalone = sum(1 for c in corpus if c["kind"] == "emoji")
        with_emoji = sum(1 for c in corpus if c["emojis"])

        # 高频词（复用 analytics 的分词，语料已是纯文本）
        from core.analytics import extract_keywords
        kw = extract_keywords(kw_texts, 40)

        # 口头禅候选：高频且不成词的组合
        verbal = [w for w, _ in kw if not w.isascii()][:20]

        # 应答词
        ack_words = ["嗯", "嗯嗯", "好", "好的", "好吧", "ok", "OK", "行", "可以", "收到",
                     "是的", "对", "是的", "谢谢", "感谢", "辛苦"]
        ack = Counter()
        for t in texts:
            s = t.strip()
            for a in ack_words:
                if s == a or s.startswith(a) and len(s) <= len(a) + 2:
                    ack[a] += 1
                    break

        # 话题
        topics = []
        for name, kws in TOPIC_LEXICON.items():
            hits = sum(1 for t in texts if any(k in t for k in kws))
            if hits:
                topics.append({"topic": name, "hits": hits, "ratio": round(hits / max(1, n), 3)})
        topics.sort(key=lambda x: -x["hits"])

        # 分人群语气
        groups = {"私聊": [], "群聊": []}
        for c in corpus:
            groups["群聊" if c["sessionType"] == "group" else "私聊"].append(c)
        crowd = {}
        for g, items in groups.items():
            if not items:
                continue
            gl = [i["len"] for i in items]
            ge = sum(1 for i in items if i["emojis"] or i["kind"] == "emoji")
            crowd[g] = {
                "messages": len(items),
                "avgLen": round(sum(gl) / len(gl), 1),
                "emojiRate": round(ge / len(items), 3),
                "topEmoji": Counter(e for i in items for e in i["emojis"]).most_common(3),
            }

        # 月度活跃
        monthly = Counter(c["time"][:7] for c in corpus if c["time"])

        return {
            "total": n,
            "span": (corpus[0]["time"], corpus[-1]["time"]) if corpus else ("", ""),
            "length": {
                "avg": round(sum(lens) / max(1, n), 1),
                "median": sorted(lens)[n // 2] if lens else 0,
                "shortRate": round(short / max(1, n), 3),
                "midRate": round(mid / max(1, n), 3),
                "longRate": round(long_ / max(1, n), 3),
                "max": max(lens) if lens else 0,
            },
            "punctuation": {
                "questionRate": round(q / max(1, n), 3),
                "exclaimRate": round(ex / max(1, n), 3),
                "ellipsisRate": round(ell / max(1, n), 3),
                "noPunctRate": round(nopunct / max(1, n), 3),
            },
            "emoji": {
                "inlineRate": round(with_emoji / max(1, n), 3),
                "standaloneRate": round(standalone / max(1, n), 3),
                "top": [{"name": k, "count": v} for k, v in emoji_counter.most_common(15)],
                "totalInline": sum(emoji_counter.values()),
                "standaloneEmoji": standalone,
            },
            "keywords": [{"word": w, "count": c} for w, c in kw],
            "verbalHabits": verbal,
            "ackWords": [{"word": w, "count": c} for w, c in ack.most_common(8)],
            "topics": topics,
            "crowd": crowd,
            "monthly": dict(sorted(monthly.items())),
        }

    # ------------------------------------------------------------ Markdown 渲染
    def render(self, p: dict, corpus: list[dict]) -> str:
        L = []
        L.append("# 我的说话风格画像（个人 bot 素材）")
        L.append("")
        L.append(f"> 语料 {p['total']:,} 条本人发言｜跨度 {p['span'][0]} ~ {p['span'][1]}")
        L.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        L.append("")
        L.append("## 一、长度与节奏")
        L.append("")
        ln = p["length"]
        L.append(f"- 平均 **{ln['avg']} 字**，中位数 {ln['median']} 字，最长 {ln['max']} 字")
        L.append(f"- 短句（≤10字）**{ln['shortRate']:.0%}**｜中等 {ln['midRate']:.0%}"
                 f"｜长句（>50字）{ln['longRate']:.0%}")
        pu = p["punctuation"]
        L.append(f"- 标点：问号 {pu['questionRate']:.0%}｜感叹号 {pu['exclaimRate']:.0%}"
                 f"｜省略号 {pu['ellipsisRate']:.0%}｜不打标点 {pu['noPunctRate']:.0%}")
        L.append("")
        L.append("## 二、表情习惯")
        L.append("")
        em = p["emoji"]
        L.append(f"- 文本内嵌表情消息占 **{em['inlineRate']:.0%}**，独立表情消息占 {em['standaloneRate']:.0%}")
        L.append(f"- 表情总出现次数 {em['totalInline']:,}，常用：")
        L.append("")
        L.append("| 表情 | 次数 |")
        L.append("|---|---|")
        for e in em["top"]:
            L.append(f"| [{e['name']}] | {e['count']} |")
        L.append("")
        L.append("## 三、口头禅与高频词")
        L.append("")
        L.append("特征词：" + "、".join(f"**{w}**" for w in p["verbalHabits"][:12]))
        L.append("")
        L.append("高频词 TOP20：" + "、".join(f"{k['word']}({k['count']})" for k in p["keywords"][:20]))
        L.append("")
        if p["ackWords"]:
            L.append("应答词：" + "、".join(f"{a['word']}({a['count']})" for a in p["ackWords"]))
            L.append("")
        L.append("## 四、话题分布")
        L.append("")
        L.append("| 话题 | 命中 | 占比 |")
        L.append("|---|---|---|")
        for t in p["topics"]:
            L.append(f"| {t['topic']} | {t['hits']} | {t['ratio']:.1%} |")
        L.append("")
        L.append("## 五、分场景语气")
        L.append("")
        L.append("| 场景 | 消息数 | 平均长度 | 表情率 | 常用表情 |")
        L.append("|---|---|---|---|---|")
        for g, d in p["crowd"].items():
            te = " ".join(f"[{n}]({c})" for n, c in d["topEmoji"])
            L.append(f"| {g} | {d['messages']:,} | {d['avgLen']} 字 | {d['emojiRate']:.0%} | {te} |")
        L.append("")
        L.append("## 六、System Prompt 草稿（可直接喂给 LLM）")
        L.append("")
        L.append("```text")
        L.append(self._prompt_draft(p))
        L.append("```")
        L.append("")
        L.append("## 七、语料样本（按场景抽样）")
        L.append("")
        L.extend(self._samples(corpus))
        L.append("")
        L.append("---")
        L.append("*由墨溯 mosu persona 生成｜全部数据来自本人发言，纯本地处理*")
        return "\n".join(L)

    def _prompt_draft(self, p: dict) -> str:
        ln, pu, em = p["length"], p["punctuation"], p["emoji"]
        top_e = "、".join(f"[{e['name']}]" for e in em["top"][:6])
        verbal = "、".join(p["verbalHabits"][:8])
        lines = [
            "你是白墨本人在微信聊天时的数字分身。用他的口吻回复，规则：",
            f"1. 长度：平均 {ln['avg']} 字，{ln['shortRate']:.0%} 的消息在 10 字以内——"
            f"默认短句，别长篇大论",
            f"2. 表情：{em['inlineRate']:.0%} 的消息带内嵌表情，常用 {top_e}；"
            f"语气词位置自然，不要每条都加",
            f"3. 口头禅：{verbal}——自然地用，别堆砌",
            f"4. 标点：问号率 {pu['questionRate']:.0%}，感叹号 {pu['exclaimRate']:.0%}"
            f"（少用），{pu['noPunctRate']:.0%} 的消息直接不打标点",
        ]
        crowd = p["crowd"]
        if "群聊" in crowd:
            lines.append(f"5. 群聊里更放松（平均 {crowd['群聊']['avgLen']} 字），"
                         f"私聊稍正式（平均 {crowd.get('私聊', {}).get('avgLen', '—')} 字）")
        lines.append("6. 不确定的事直接说不知道，别编；回复像微信消息，不像邮件")
        return "\n".join(lines)

    def _samples(self, corpus: list[dict], per: int = 6) -> list[str]:
        out = []
        # 最短的（应答型）、最长的（叙述型）、带表情的、无标点的
        pools = {
            "短句应答（≤6字）": [c for c in corpus if c["len"] <= 6 and c["kind"] == "text"],
            "带表情的中长句": [c for c in corpus if c["emojis"] and c["len"] > 12],
            "长叙述（>60字）": [c for c in corpus if c["len"] > 60],
            "提问句": [c for c in corpus if "？" in c["text"] or "?" in c["text"]],
        }
        for title, pool in pools.items():
            if not pool:
                continue
            step = max(1, len(pool) // per)
            picked = pool[::step][:per]
            out.append(f"**{title}**")
            out.append("")
            for c in picked:
                out.append(f"- `{c['session']}` {c['text'][:80]}")
            out.append("")
        return out


def build_persona(ds, out_dir: str | Path) -> dict:
    pb = PersonaBuilder(ds)
    out_dir = Path(out_dir)
    corpus = pb.collect()
    stats = pb.write_corpus(corpus, out_dir)
    profile = pb.build_profile(corpus)
    md = pb.render(profile, corpus)
    (out_dir / "style_profile.md").write_text(md, encoding="utf-8")
    return {"corpus": stats, "profile": {"total": profile["total"],
                                         "avgLen": profile["length"]["avg"],
                                         "topEmoji": profile["emoji"]["top"][:5]},
            "styleProfile": str(out_dir / "style_profile.md")}
