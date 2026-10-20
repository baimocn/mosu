# -*- coding: utf-8 -*-
"""
墨溯 core —— 画像蒸馏（M5）

把一堆消息蒸馏成"人能读懂的画像"，两种模式：

  1. self_profile()     全库个人画像：作息 / 社交图谱 / 话题 / 表达风格 / 月度 / 综合侧写
                        （结构对标 2026-09-25 已验证的个人画像报告）
  2. contact_profile()  单联系人深度画像：互动概览 / 节律对比 / 发起与回复 /
                        话题对比 / 情感曲线 / 亲密度信号 / 里程碑 / 关键事件时间线

全部纯本地统计 + 规则推断，不调用外部 LLM（开源用户离线可用）。
输出结构化 dict，render_markdown() 渲染成 Markdown 报告。
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import datetime

from core.analytics import clean_text, extract_keywords
from core.dataset import KIND_BY_TYPE
from core.sentiment import score_text, score_texts, sentiment_curve

# ---------------------------------------------------------------- 主题词典
TOPIC_LEXICON: dict[str, list[str]] = {
    "AI/编程": ["python", "api", "模型", "deepseek", "gpt", "claude", "代码", "部署", "服务器",
                "脚本", "开源", "github", "token", "prompt", "agent", "智能体", "训练", "微调",
                "数据库", "sqlite", "node", "docker", "接口", "调试", "bug", "报错"],
    "考研/学业": ["考研", "数学", "英语", "政治", "专业课", "自习", "图书馆", "本科", "自考",
                 "论文", "答辩", "期末", "考试", "挂科", "报名", "网课", "刷题", "背书"],
    "项目/产品": ["项目", "产品", "上线", "版本", "需求", "原型", "设计", "测试", "迭代",
                 "用户", "客户", "反馈", "文档", "排期", "交付"],
    "生意/接单": ["报价", "多少钱", "价格", "付款", "收款", "定金", "尾款", "接单", "私活",
                 "合同", "发票", "对公", "咸鱼", "闲鱼", "店铺", "客户", "乙方"],
    "校园/宿舍": ["宿舍", "食堂", "上课", "老师", "同学", "班级", "辅导员", "军训", "社团",
                 "学生会", "实验", "早八", "点名", "作业", "期末"],
    "游戏/娱乐": ["游戏", "王者", "原神", "吃鸡", "上号", "开黑", "副本", "排位", "皮肤",
                 " Steam", "switch", "番剧", "电影", "追剧", "综艺"],
    "运动/健康": ["跑步", "健身", "锻炼", "打球", "篮球", "足球", "羽毛球", "游泳", "爬山",
                 "步数", "减肥", "早睡", "熬夜", "生病", "感冒", "发烧", "体检"],
    "情感/关系": ["喜欢", "表白", "恋爱", "对象", "女朋友", "男朋友", "暧昧", "暗恋", "分手",
                 "复合", "相亲", "单身", "心动", "吃醋", "想念", "晚安"],
    "生活/日常": ["吃饭", "外卖", "奶茶", "咖啡", "快递", "购物", "逛街", "天气", "下雨",
                 "降温", "堵车", "放假", "旅游", "回家", "机票", "高铁"],
    "工作/职场": ["上班", "加班", "工资", "绩效", "离职", "入职", "面试", "简历", "老板",
                 "同事", "周报", "开会", " KPI", "调休", "年假"],
}

# SQLite %w：0=周日 … 6=周六。下标必须按 %w 的定义，否则星期整体偏移一天
WEEKDAY_NAMES = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]


def _bar(value: int, vmax: int, width: int = 20) -> str:
    if vmax <= 0:
        return ""
    n = max(1, round(value / vmax * width)) if value else 0
    return "█" * n


def _ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def _day(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return "-"


class ProfileDistiller:
    def __init__(self, ds):
        self.ds = ds

    # ============================================================ 数据收集
    def _collect(self, sess=None, from_ts=None, to_ts=None, max_msgs: int = 60000,
                 newest_first: bool = True, per_session: int = 300) -> list[dict]:
        """遍历消息，统一成 [{ts, kind, text, direction, sender, session}]。

        全库时按会话分层采样（每会话最多 per_session 条，按时间倒序），
        避免被单个超大群（如 14 万条的 AI 群）挤占样本。
        """
        if sess is not None:
            targets = [sess]
        else:
            targets = list(self.ds.sessions())
            if newest_first:
                targets.sort(key=lambda s: s.get("sortTime") or s.get("lastTime") or 0,
                             reverse=True)
        out: list[dict] = []
        # 触顶标记：全库画像同样受 max_msgs 限制（默认 60000），23 万条库一定会被截。
        # 之前这个信息只存在于"提前 return"这个控制流里，返回值里看不到，
        # 于是本人画像页永远不会提示用户"你看到的只是样本"。
        truncated = False
        for s in targets:
            if sess is None and len(out) >= max_msgs:
                truncated = True
                break
            n_sess = 0
            cap = per_session if sess is None else 0
            # 每会话只取**最近** cap 条。iter_session_rows 默认升序，直接
            # 数到 cap 就 break 拿到的是会话最早的 cap 条（大群如 14 万条的
            # AI 群会因此只贡献开头部分，话题/风格统计严重偏斜）。
            picked: list[tuple] = []
            for shard, r in self.ds.iter_session_rows(
                    s, from_ts, to_ts, desc=bool(cap), limit=cap or 0):
                if len(out) >= max_msgs:
                    truncated = True
                    return out, truncated
                if cap and len(picked) >= cap:
                    break
                picked.append((shard, r))
            if cap and len(picked) > cap:      # 跨分片合并后再截断
                picked.sort(key=lambda sr: sr[1]["create_time"] or 0)
                picked = picked[-cap:]
            # 该会话实际还有更多消息，但我们只取了最近 cap 条 —— 同样是截断
            if cap and len(picked) >= cap:
                truncated = True
            for shard, r in picked:
                n_sess += 1
                m = self.ds.to_message(r, s, shard=shard)
                kind = m.get("kind") or "other"
                out.append({
                    "ts": m.get("createTime"),
                    "kind": kind,
                    "text": "" if kind == "emoji" else clean_text(m.get("content")),
                    "direction": m.get("direction") or "unknown",
                    "sender": m.get("senderName"),
                    "session": s["displayName"],
                    "imgMd5": m.get("imgMd5"),
                    "voiceMs": m.get("voiceMs"),
                    "title": m.get("title"),
                })
        return out, truncated

    # ============================================================ 全库画像
    # ============================================================ 全库画像
    def _sql_aggregate(self, from_ts=None, to_ts=None) -> dict:
        """全库 SQL 聚合：节律/社交图谱/月度/类型构成（不解压消息体，秒级）。"""
        ds = self.ds
        hour_t: Counter = Counter(); hour_m: Counter = Counter()
        wd_t: Counter = Counter(); wd_m: Counter = Counter()
        mon_t: Counter = Counter(); mon_m: Counter = Counter()
        kinds: Counter = Counter(); kinds_mine: Counter = Counter()
        social: dict[str, dict] = defaultdict(
            lambda: {"mine": 0, "total": 0, "name": ""})
        total = mine_n = 0
        day_set: set = set()
        first = last = None
        for sess in ds.sessions():
            for sh in ds._shard_list(sess):
                con = ds.conn(sh["shard"])
                where, params = [], []
                if from_ts:
                    where.append("create_time >= ?"); params.append(from_ts)
                if to_ts:
                    where.append("create_time <= ?"); params.append(to_ts)
                w = (" WHERE " + " AND ".join(where)) if where else ""
                sid = ds.self_id(sh["shard"]); t = sh["table"]
                r = con.execute(
                    f'SELECT count(*), min(create_time), max(create_time), '
                    f'SUM(CASE WHEN real_sender_id=? THEN 1 ELSE 0 END) '
                    f'FROM "{t}"{w}', params + [sid]).fetchone()
                total += r[0] or 0; mine_n += r[3] or 0
                if r[1]:
                    first = r[1] if first is None else min(first, r[1])
                if r[2]:
                    last = r[2] if last is None else max(last, r[2])
                d = social[sess["sessionId"]]      # 用 wxid 作键：两个同名会话不该被合并
                d["name"] = sess["displayName"]
                d["total"] += r[0] or 0; d["mine"] += r[3] or 0
                sql = (f"SELECT strftime('%H', create_time,'unixepoch','localtime'), "
                       f"strftime('%w', create_time,'unixepoch','localtime'), "
                       f"strftime('%Y-%m', create_time,'unixepoch','localtime'), count(*), "
                       f"SUM(CASE WHEN real_sender_id=? THEN 1 ELSE 0 END) "
                       f'FROM "{t}"{w} GROUP BY 1,2,3')
                for h, wd, mk, c, cm in con.execute(sql, params + [sid]):
                    hour_t[int(h)] += c; hour_m[int(h)] += cm or 0
                    wd_t[int(wd)] += c; wd_m[int(wd)] += cm or 0
                    mon_t[mk] += c; mon_m[mk] += cm or 0
                day_set.update(x[0] for x in con.execute(
                    f"SELECT DISTINCT strftime('%Y-%m-%d', create_time,'unixepoch','localtime') "
                    f'FROM "{t}"{w}', params))
                for lt, c, cm in con.execute(
                        f'SELECT local_type, count(*), '
                        f'SUM(CASE WHEN real_sender_id=? THEN 1 ELSE 0 END) '
                        f'FROM "{t}"{w} GROUP BY local_type', params + [sid]):
                    k = KIND_BY_TYPE.get(lt & 0xFFFFFFFF, "other")
                    kinds[k] += c
                    kinds_mine[k] += cm or 0
        return {"hour_t": hour_t, "hour_m": hour_m, "wd_t": wd_t, "wd_m": wd_m,
                "mon_t": mon_t, "mon_m": mon_m, "kinds": kinds, "kinds_mine": kinds_mine,
                "social": social,
                "total": total, "mine": mine_n, "days": len(day_set),
                "first": first, "last": last}

    def self_profile(self, from_ts=None, to_ts=None, max_msgs: int = 60000,
                     top: int = 15, per_session: int = 300) -> dict:
        t0 = time.time()
        agg = self._sql_aggregate(from_ts, to_ts)
        msgs, truncated = self._collect(None, from_ts, to_ts, max_msgs,
                                        per_session=per_session)   # 仅用于话题/关键词
        mine = [m for m in msgs if m["direction"] == "out"]

        hour_total, hour_mine = agg["hour_t"], agg["hour_m"]
        wd_total, wd_mine = agg["wd_t"], agg["wd_m"]
        month_total, month_mine = agg["mon_t"], agg["mon_m"]
        kinds, days = agg["kinds"], agg["days"]
        kinds_mine = agg["kinds_mine"]
        total, mine_n = agg["total"], agg["mine"]

        peak_hour = hour_total.most_common(1)[0][0] if hour_total else None
        peak_wd = wd_total.most_common(1)[0][0] if wd_total else None
        quiet4 = min(range(21), key=lambda s: sum(hour_total.get((s + i) % 24, 0) for i in range(4))) \
            if hour_total else None
        night = sum(hour_mine.get(h, 0) for h in range(0, 6))       # 0-5 点
        morning = sum(hour_mine.get(h, 0) for h in range(6, 10))   # 6-9 点（与夜间不重叠）

        social = sorted(
            [{"wxid": k, "name": v["name"] or k, "mine": v["mine"], "total": v["total"],
              "share": round(v["mine"] / v["total"], 2) if v["total"] else 0}
             for k, v in agg["social"].items() if v["mine"] > 0],
            key=lambda x: -x["mine"])[:top]

        texts = [m["text"] for m in mine if m["text"]]
        topics = []
        for name, kws in TOPIC_LEXICON.items():
            hits = sum(1 for t in texts if any(k in t for k in kws))
            if hits:
                topics.append({"topic": name, "hits": hits,
                               "ratio": round(hits / max(1, len(texts)), 3)})
        topics.sort(key=lambda x: -x["hits"])
        keywords = [{"word": w, "count": c} for w, c in extract_keywords(texts, 30)]
        bigrams = self._bigrams(texts, 20)

        lens = [len(t) for t in texts if t]
        emoji_rate = kinds_mine.get("emoji", 0) / max(1, mine_n)
        q_rate = sum(1 for t in texts
                   if "?" in t or "？" in t) / max(1, len(texts))
        insights = self._self_insights(hour_mine, night, mine_n, lens, topics,
                                       emoji_rate, q_rate, peak_wd)

        span = (agg["last"] - agg["first"]) / 86400 if agg["first"] and agg["last"] else 0
        return {
            "scope": "self",
            "meta": {"scannedMessages": total, "textSampled": len(msgs),
                     "myMessages": mine_n, "cost": round(time.time() - t0, 1),
                     # 与联系人画像同一套契约：话题/词频/风格来自采样，
                     # 触顶时必须告知，否则用户会把样本当全量
                     "truncated": truncated, "maxMsgs": max_msgs},
            "overview": {
                "totalMessages": total, "myMessages": mine_n,
                "myShare": round(mine_n / max(1, total), 3),
                "firstText": _ts(agg["first"]) if agg["first"] else None,
                "lastText": _ts(agg["last"]) if agg["last"] else None,
                "activeDays": days,
                # float：与 contact_profile 的 spanDays 保持同类型。
                # JS 里 3 !== 3.0 为 true，跨页比较时类型不一致会判错分段。
                "spanDays": float(round(span, 1)),
            },
            "rhythm": {
                "hourly": [{"hour": h, "total": hour_total.get(h, 0),
                            "mine": hour_mine.get(h, 0)} for h in range(24)],
                "weekday": [{"day": WEEKDAY_NAMES[i], "total": wd_total.get(i, 0),
                             "mine": wd_mine.get(i, 0)} for i in range(7)],
                "peakHour": peak_hour,
                "peakWeekday": WEEKDAY_NAMES[peak_wd] if peak_wd is not None else None,
                "quietest4hStart": quiet4,
                "nightMessages": night, "morningMessages": morning,
            },
            "social": social,
            "topics": {"byTopic": topics, "keywords": keywords, "bigrams": bigrams},
            "style": {
                "avgLength": round(sum(lens) / max(1, len(lens)), 1),
                "byKind": dict(kinds_mine.most_common()),
                "emojiRate": round(emoji_rate, 3),
                "questionRate": round(q_rate, 3),
            },
            "monthly": [{"month": k, "mine": month_mine.get(k, 0), "total": month_total.get(k, 0)}
                        for k in sorted(month_total)],
            "insights": insights,
        }

    def _bigrams(self, texts, top: int = 20) -> list[dict]:
        freq: Counter = Counter()
        for t in texts:
            if len(t) < 2:
                continue
            for i in range(len(t) - 1):
                bg = t[i:i + 2]
                if all("\u4e00" <= c <= "\u9fff" for c in bg):
                    freq[bg] += 1
        stop = {"的一", "了我", "是你", "的一", "个的", "了的", "是不", "不是", "就是", "这个",
                "那个", "什么", "怎么", "可以", "现在", "一个", "我们", "你们", "他们"}
        out = [(w, c) for w, c in freq.most_common(200) if w not in stop]
        return [{"word": w, "count": c} for w, c in out[:top]]

    def _self_insights(self, hour_mine, night, my_total, lens, topics,
                       emoji_rate, q_rate, peak_wd) -> list[str]:
        out = []
        if my_total and night / my_total > 0.08:
            out.append(f"夜猫子作息：凌晨 0-5 点的本人消息占 {night/my_total:.0%}")
        if lens and sum(lens) / len(lens) < 15:
            out.append(f"短句派：本人平均消息 {sum(lens)/len(lens):.1f} 字，沟通直接")
        elif lens and sum(lens) / len(lens) > 40:
            out.append(f"长文型：本人平均消息 {sum(lens)/len(lens):.1f} 字，习惯成段表达")
        if q_rate > 0.12:
            out.append(f"提问型沟通：{q_rate:.0%} 的消息带问号，偏信息收集/推进型")
        if emoji_rate > 0.08:
            out.append(f"表情包高频：{emoji_rate:.0%} 的消息是表情，聊天氛围偏轻松")
        hit = {t["topic"]: t["hits"] for t in topics[:3]}
        if hit.get("AI/编程"):
            out.append("技术/开发者身份显著：AI、编程、部署类话题高频")
        if hit.get("生意/接单"):
            out.append("经营线并行：客户/报价/交付类对话成规模，不是纯学生聊天结构")
        if hit.get("考研/学业"):
            out.append("学业线存在：考研/课程话题有持续痕迹")
        if peak_wd is not None:
            # 只陈述被计算过的事实：峰值星期。不由此推断"周末高于工作日"——
            # 那个结论需要单独比较两组总量，这里没有算。
            out.append(f"{WEEKDAY_NAMES[peak_wd]}是最活跃的一天")
        return out

    # ============================================================ 单人画像
    # 单会话画像的样本上限。14 万条的大群逐条解压要几分钟、内存数百 MB，
    # 而 GUI 侧是同步持锁调用——不限上限就是唯一能打挂整个服务的一条路径。
    # 命中上限时 overview.truncated=True，由调用方告知用户「只基于最近 N 条」。
    CONTACT_MAX_MSGS = 60000

    def contact_profile(self, query: str, from_ts=None, to_ts=None,
                        top: int = 10, max_msgs: int | None = None) -> dict:
        t0 = time.time()
        cap = self.CONTACT_MAX_MSGS if max_msgs is None else int(max_msgs)
        sess = self.ds.find_session(query)
        if not sess:
            raise SystemExit(f"[x] 没找到会话「{query}」，先跑 `mosu sessions` 看看")
        # order="desc"：取**最近** cap 条。画像的用途是"了解这个人现在的样子"，
        # 取最早 cap 条等于让两年前的聊天决定今天的画像——与 _collect 曾经踩过的
        # 「采样取到最早 N 条」是同一类错误。
        rows, truncated = self.ds.raw_messages_page(
            sess, limit=cap, from_ts=from_ts, to_ts=to_ts, order="desc")
        msgs = []
        for shard, r in rows:
            m = self.ds.to_message(r, sess, shard=shard)
            _k = m.get("kind") or "other"
            msgs.append({"ts": m.get("createTime"), "kind": _k,
                         "text": "" if _k == "emoji" else clean_text(m.get("content")),
                         "direction": m.get("direction") or "unknown",
                         "imgMd5": m.get("imgMd5"), "voiceMs": m.get("voiceMs"),
                         "title": m.get("title")})
        if not msgs:
            raise SystemExit(f"[x] 会话「{sess['displayName']}」在所选时间范围内没有消息")
        # 取数用 desc（拿最近 cap 条），但下游统计一律按**时间升序**消费：
        # milestones.firstMessage / lastMessage 取 msgs[0] 与 msgs[-1]，
        # 发起模式要顺着时间轴走间隔，月份聚合也依赖顺序。
        # 这里排回升序，让"取最近"与"按序统计"两件事各归各。
        msgs.sort(key=lambda m: m["ts"] or 0)
        # 截断后样本的"最早一条"已不是真实首条，里程碑要如实标注取的是哪一段
        sample_first, sample_last = _ts(msgs[0]["ts"]), _ts(msgs[-1]["ts"])
        mine = [m for m in msgs if m["direction"] == "out"]
        theirs = [m for m in msgs if m["direction"] != "out"]
        days = {_day(m["ts"]) for m in msgs if m["ts"]}

        # ---- 节律对比
        h_mine: Counter = Counter()
        h_them: Counter = Counter()
        monthly: Counter = Counter()
        for m in msgs:
            if not m["ts"]:
                continue
            dt = datetime.fromtimestamp(m["ts"])
            monthly[dt.strftime("%Y-%m")] += 1
            if m["direction"] == "out":
                h_mine[dt.hour] += 1
            else:
                h_them[dt.hour] += 1

        # ---- 发起模式（间隔 > 6h 视为新一轮对话）
        init_mine = init_them = 0
        lat_mine: list[int] = []      # 我回对方的间隔
        lat_them: list[int] = []      # 对方回我的间隔
        prev = None
        for m in msgs:
            if not m["ts"]:
                continue
            if prev is None or m["ts"] - prev["ts"] > 6 * 3600:
                if m["direction"] == "out":
                    init_mine += 1
                else:
                    init_them += 1
            elif prev["direction"] != m["direction"]:
                gap = m["ts"] - prev["ts"]
                if gap < 12 * 3600:
                    (lat_them if m["direction"] != "out" else lat_mine).append(gap)
            prev = m

        # ---- 话题对比
        kw_mine = [{"word": w, "count": c}
                   for w, c in extract_keywords([m["text"] for m in mine if m["text"]], top)]
        kw_them = [{"word": w, "count": c}
                   for w, c in extract_keywords([m["text"] for m in theirs if m["text"]], top)]
        topic_mine = self._topic_hits([m["text"] for m in mine if m["text"]])
        topic_them = self._topic_hits([m["text"] for m in theirs if m["text"]])

        # ---- 情感
        sent_mine = score_texts([m["text"] for m in mine if m["text"]])
        sent_them = score_texts([m["text"] for m in theirs if m["text"]])
        curve = sentiment_curve([(m["ts"], m["text"]) for m in msgs if m["text"]])

        # ---- 里程碑
        milestones = {
            "firstMessage": {"time": sample_first, "who": "我" if msgs[0]["direction"] == "out" else "对方",
                             "content": msgs[0]["text"][:60]},
            "lastMessage": {"time": sample_last, "who": "我" if msgs[-1]["direction"] == "out" else "对方",
                            "content": msgs[-1]["text"][:60]},
            "firstVoice": self._first(msgs, "voice"),
            "firstImage": self._first(msgs, "image"),
            "firstVideo": self._first(msgs, "video"),
            "busiestDay": self._busiest_day(msgs),
        }
        if truncated:
            # 截断时样本内「最早的一条」不是会话真实首条。firstMessage 与三个
            # first* 里程碑**同属这个问题**，必须一起标——只标一个反而更危险：
            # 页面上「首条消息」被谨慎标注「样本内」，紧挨着的「第一条语音」却仍是
            # 确定语气，用户完全有理由读成「他们第一次发语音是那天」。
            milestones["firstMessage"]["sampledOnly"] = True
            for key in ("firstVoice", "firstImage", "firstVideo"):
                if milestones.get(key):
                    milestones[key]["sampledOnly"] = True

        # ---- 亲密度信号
        signals = self._signals(msgs, mine, theirs)

        # ---- 关键事件时间线（消息量 top 月 + 当月首条）
        timeline = self._timeline(msgs, monthly)

        return {
            "scope": "contact",
            "session": {"sessionId": sess["sessionId"], "displayName": sess["displayName"],
                        "type": sess["type"]},
            "meta": {"scannedMessages": len(msgs), "cost": round(time.time() - t0, 1),
                     "truncated": truncated, "maxMsgs": cap},
            "overview": {
                "totalMessages": len(msgs),
                "myMessages": len(mine),
                "theirMessages": len(theirs),
                "myShare": round(len(mine) / max(1, len(msgs)), 3),
                "activeDays": len(days),
                "firstText": _ts(msgs[0]["ts"]),
                "lastText": _ts(msgs[-1]["ts"]),
                # float 而非 int：JS 里 3 !== 3.0 为 true，跨页比较 spanDays
                # （本人页 vs 联系人页）会因类型不同而走进错误分段
                "spanDays": float(round((msgs[-1]["ts"] - msgs[0]["ts"]) / 86400, 1)),
                "truncated": truncated,
            },
            "rhythm": {
                "hourlyMine": [h_mine.get(h, 0) for h in range(24)],
                "hourlyTheirs": [h_them.get(h, 0) for h in range(24)],
                "monthly": [{"month": k, "count": v} for k, v in sorted(monthly.items())],
            },
            "interaction": {
                "initiatedByMe": init_mine,
                "initiatedByThem": init_them,
                "myAvgReplySeconds": int(sum(lat_mine) / len(lat_mine)) if lat_mine else None,
                "theirAvgReplySeconds": int(sum(lat_them) / len(lat_them)) if lat_them else None,
            },
            "topics": {"mine": topic_mine, "theirs": topic_them,
                       "keywordsMine": kw_mine, "keywordsTheirs": kw_them},
            "sentiment": {"mine": sent_mine, "theirs": sent_them, "curve": curve},
            "signals": signals,
            "milestones": milestones,
            "timeline": timeline,
        }

    def _topic_hits(self, texts) -> list[dict]:
        out = []
        for name, kws in TOPIC_LEXICON.items():
            hits = sum(1 for t in texts if any(k in t for k in kws))
            if hits:
                out.append({"topic": name, "hits": hits,
                            "ratio": round(hits / max(1, len(texts)), 3)})
        out.sort(key=lambda x: -x["hits"])
        return out[:8]

    @staticmethod
    def _first(msgs, kind):
        for m in msgs:
            if m["kind"] == kind:
                return {"time": _ts(m["ts"]), "who": "我" if m["direction"] == "out" else "对方"}
        return None

    @staticmethod
    def _busiest_day(msgs):
        cnt: Counter = Counter(_day(m["ts"]) for m in msgs if m["ts"])
        if not cnt:
            return None
        d, c = cnt.most_common(1)[0]
        sample = next((m["text"][:40] for m in msgs if _day(m["ts"]) == d and m["text"]), "")
        return {"day": d, "count": c, "sample": sample}

    def _signals(self, msgs, mine, theirs) -> dict:
        texts = [m["text"] for m in msgs if m["text"]]
        emoji = sum(1 for m in msgs if m["kind"] == "emoji")
        laugh = sum(t.count("哈") for t in texts)
        half = max(1, len(msgs) // 2)
        first_lens = [len(m["text"]) for m in msgs[:half] if m["text"]]
        last_lens = [len(m["text"]) for m in msgs[half:] if m["text"]]
        trend = None
        if first_lens and last_lens:
            a, b = sum(first_lens) / len(first_lens), sum(last_lens) / len(last_lens)
            trend = "变长" if b > a * 1.15 else ("变短" if b < a * 0.85 else "持平")
        return {
            "emojiRate": round(emoji / max(1, len(msgs)), 3),
            "laughPer100": round(laugh / max(1, len(texts)) * 100, 1),
            "lengthTrend": trend,
            "mediaShare": round(sum(1 for m in msgs if m["kind"] != "text") / max(1, len(msgs)), 3),
            "voiceMine": sum(1 for m in mine if m["kind"] == "voice"),
            "voiceTheirs": sum(1 for m in theirs if m["kind"] == "voice"),
            "imageMine": sum(1 for m in mine if m["kind"] == "image"),
            "imageTheirs": sum(1 for m in theirs if m["kind"] == "image"),
        }

    def _timeline(self, msgs, monthly: Counter) -> list[dict]:
        if not monthly:
            return []
        top_months = [k for k, _ in monthly.most_common(3)]
        out = []
        for k in top_months:
            month_msgs = [m for m in msgs if m["ts"] and _day(m["ts"]).startswith(k)]
            first_text = next((m["text"][:50] for m in month_msgs if m["text"]), "")
            peak = self._busiest_day(month_msgs)
            out.append({"month": k, "count": len(month_msgs), "firstText": first_text,
                        "peakDay": peak})
        return out

    # ============================================================ Markdown 渲染
    def render_markdown(self, data: dict) -> str:
        if data.get("scope") == "self":
            return self._render_self(data)
        return self._render_contact(data)

    # ---------------- 全库
    def _render_self(self, d: dict) -> str:
        o, r, st, tp, sty = (d["overview"], d["rhythm"], d["social"],
                             d["topics"], d["style"])
        L = []
        L.append(f"# {self.ds.self_username() or '本人'} · 微信数据画像")
        L.append("")
        L.append(f"> 数据源：{o['totalMessages']} 条消息全量统计"
                 f"（话题/词频基于最近 {d['meta']['textSampled']} 条采样）"
                 f"｜本人发言 {o['myMessages']} 条（{o['myShare']:.0%}）"
                 f"｜跨度 {o['firstText']} ~ {o['lastText']}（{int(o['spanDays'])} 天）"
                 f"｜活跃 {o['activeDays']} 天")
        L.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}｜耗时 {d['meta']['cost']}s")
        L.append("")

        L.append("## 一、作息画像")
        L.append("")
        hmax = max((x["total"] for x in r["hourly"]), default=0)
        L.append("| 时段 | 活跃度 | 全部（我） |")
        L.append("|---|---|---|")
        for x in r["hourly"]:
            L.append(f"| {x['hour']:02d}:00 | {_bar(x['total'], hmax)} | {x['total']}（我 {x['mine']}）|")
        L.append("")
        # 空库时 peakHour/peakWeekday 都是 None，直接格式化会 TypeError
        peak_h = f"{r['peakHour']:02d}:00" if r.get("peakHour") is not None else "无数据"
        peak_w = r.get("peakWeekday") or "无数据"
        L.append(f"- **发言高峰**：{peak_h}｜**最活跃星期**：{peak_w}")
        if r["quietest4hStart"] is not None:
            L.append(f"- **睡眠推断**：{r['quietest4hStart']:02d}:00–"
                     f"{(r['quietest4hStart']+4)%24:02d}:00 为最安静窗口")
        L.append(f"- **深夜活动**（00-06 点，本人）：{r['nightMessages']} 条"
                 f"｜**早间**（05-09 点，本人）：{r['morningMessages']} 条")
        L.append("")

        L.append("## 二、社交图谱（按我发出的消息量）")
        L.append("")
        L.append("| 会话 | 我的发言 | 会话总量 | 我的占比 |")
        L.append("|---|---|---|---|")
        for s in st:
            L.append(f"| {s['name'][:18]} | {s['mine']} | {s['total']} | {s['share']:.0%} |")
        L.append("")

        L.append("## 三、兴趣与话题")
        L.append("")
        L.append("| 主题 | 命中消息（我的发言中） | 占比 |")
        L.append("|---|---|---|")
        for t in tp["byTopic"]:
            L.append(f"| {t['topic']} | {t['hits']} | {t['ratio']:.1%} |")
        L.append("")
        L.append("**高频词 TOP 20**：" + "、".join(f"{k['word']}({k['count']})"
                                                  for k in tp["keywords"][:20]))
        L.append("")
        L.append("**高频二字组合 TOP 15**：" + "、".join(f"{k['word']}({k['count']})"
                                                       for k in tp["bigrams"][:15]))
        L.append("")

        L.append("## 四、表达风格")
        L.append("")
        L.append(f"- **平均消息长度**：{sty['avgLength']} 字")
        L.append("- **消息类型构成**：" + "、".join(f"{k} {v}" for k, v in sty["byKind"].items()))
        L.append(f"- **表情包使用率**：{sty['emojiRate']:.1%}"
                 f"｜**提问率**：{sty['questionRate']:.1%}")
        L.append("")

        L.append("## 五、月度活跃（本人发言）")
        L.append("")
        mmax = max((x["mine"] for x in d["monthly"]), default=0)
        L.append("| 月份 | 条数 | 图 |")
        L.append("|---|---|---|")
        for x in d["monthly"]:
            L.append(f"| {x['month']} | {x['mine']} | {_bar(x['mine'], mmax, 24)} |")
        L.append("")

        L.append("## 六、综合侧写（由数据推断，供参考）")
        L.append("")
        for i in d["insights"]:
            L.append(f"- {i}")
        L.append("")
        L.append("---")
        L.append("*由墨溯 mosu profile 生成｜纯本地统计，无外部调用*")
        return "\n".join(L)

    # ---------------- 单联系人
    def _render_contact(self, d: dict) -> str:
        s, o, rh, it, tp = d["session"], d["overview"], d["rhythm"], d["interaction"], d["topics"]
        sg, ms, tl = d["sentiment"], d["signals"], d["timeline"]
        L = []
        L.append(f"# {s['displayName']} · 互动画像")
        L.append("")
        L.append(f"> {s['sessionId']}（{s['type']}）｜{o['totalMessages']} 条消息"
                 f"｜我 {o['myMessages']} / 对方 {o['theirMessages']}（我占 {o['myShare']:.0%}）")
        L.append(f"> 跨度 {o['firstText']} ~ {o['lastText']}（{o['spanDays']} 天）"
                 f"｜活跃 {o['activeDays']} 天｜耗时 {d['meta']['cost']}s")
        L.append("")

        L.append("## 一、互动概览")
        L.append("")
        fm, lm = d["milestones"]["firstMessage"], d["milestones"]["lastMessage"]
        # 截断时样本内最早 ≠ 会话真实最早，文案必须带限定，否则 Markdown 报告里
        # 会写出未加限定的「第一句话」，与 JSON 返回的口径不一致（同一个假数据问题，
        # JSON 那侧已标 sampledOnly）。最近一句不受影响——它取的是样本末尾＝最新。
        _smp = "（样本内，非真实首条）" if fm.get("sampledOnly") else ""
        L.append(f"- **第一句话**{_smp}（{fm['time']}，{fm['who']}）：{fm['content']}")
        L.append(f"- **最近一句**（{lm['time']}，{lm['who']}）：{lm['content']}")
        for key, label in (("firstVoice", "第一条语音"), ("firstImage", "第一张图"),
                           ("firstVideo", "第一个视频")):
            v = d["milestones"].get(key)
            if v:
                tag = "（样本内，非真实首条）" if v.get("sampledOnly") else ""
                L.append(f"- **{label}**{tag}：{v['time']}（{v['who']}）")
        bd = d["milestones"].get("busiestDay")
        if bd:
            L.append(f"- **话最多的一天**：{bd['day']} 共 {bd['count']} 条"
                     + (f"（代表句：{bd['sample']}）" if bd["sample"] else ""))
        L.append("")

        L.append("## 二、发起与回复")
        L.append("")
        total_init = it["initiatedByMe"] + it["initiatedByThem"] or 1
        L.append(f"- **谁更常开口**：我 {it['initiatedByMe']} 次"
                 f"（{it['initiatedByMe']/total_init:.0%}）／对方 {it['initiatedByThem']} 次")
        if it["theirAvgReplySeconds"] is not None:
            L.append(f"- **对方平均回我**：{self._dur(it['theirAvgReplySeconds'])}")
        if it["myAvgReplySeconds"] is not None:
            L.append(f"- **我平均回对方**：{self._dur(it['myAvgReplySeconds'])}")
        L.append("")

        L.append("## 三、时段与月度")
        L.append("")
        hmax = max(max(rh["hourlyMine"] or [0]), max(rh["hourlyTheirs"] or [0]), 1)
        L.append("| 时段 | 我 | 对方 |")
        L.append("|---|---|---|")
        for h in range(24):
            if rh["hourlyMine"][h] or rh["hourlyTheirs"][h]:
                L.append(f"| {h:02d}:00 | {_bar(rh['hourlyMine'][h], hmax, 12)} "
                         f"{rh['hourlyMine'][h]} | {_bar(rh['hourlyTheirs'][h], hmax, 12)} "
                         f"{rh['hourlyTheirs'][h]} |")
        L.append("")
        mmax = max((x["count"] for x in rh["monthly"]), default=1)
        L.append("| 月份 | 消息量 |")
        L.append("|---|---|")
        for x in rh["monthly"]:
            L.append(f"| {x['month']} | {_bar(x['count'], mmax, 24)} {x['count']} |")
        L.append("")

        L.append("## 四、话题对比")
        L.append("")
        L.append("| 主题 | 我的发言 | 对方发言 |")
        L.append("|---|---|---|")
        all_topics = {t["topic"] for t in tp["mine"]} | {t["topic"] for t in tp["theirs"]}
        mi = {t["topic"]: t["hits"] for t in tp["mine"]}
        th = {t["topic"]: t["hits"] for t in tp["theirs"]}
        for t in sorted(all_topics, key=lambda x: -(mi.get(x, 0) + th.get(x, 0))):
            L.append(f"| {t} | {mi.get(t, 0)} | {th.get(t, 0)} |")
        L.append("")
        L.append("我的高频词：" + "、".join(f"{k['word']}({k['count']})" for k in tp["keywordsMine"][:10]))
        L.append("")
        L.append("对方高频词：" + "、".join(f"{k['word']}({k['count']})" for k in tp["keywordsTheirs"][:10]))
        L.append("")

        L.append("## 五、情感倾向")
        L.append("")
        L.append(f"- **我**：均分 {sg['mine']['avg']:+.2f}"
                 f"（正面 {sg['mine']['positive']} / 负面 {sg['mine']['negative']}"
                 f" / 中性 {sg['mine']['neutral']}）")
        L.append(f"- **对方**：均分 {sg['theirs']['avg']:+.2f}"
                 f"（正面 {sg['theirs']['positive']} / 负面 {sg['theirs']['negative']}"
                 f" / 中性 {sg['theirs']['neutral']}）")
        if sg["curve"]:
            L.append("")
            L.append("| 月份 | 情感均分 | 消息数 |")
            L.append("|---|---|---|")
            for c in sg["curve"]:
                L.append(f"| {c['bucket']} | {c['avg']:+.2f} | {c['count']} |")
        L.append("")

        L.append("## 六、亲密度信号（代理指标）")
        L.append("")
        L.append(f"- **表情包率**：{ms['emojiRate']:.1%}｜**笑声密度**：每百条 {ms['laughPer100']} 个「哈」")
        L.append(f"- **消息长度趋势**（后半 vs 前半）：{ms['lengthTrend']}"
                 f"｜**媒体占比**：{ms['mediaShare']:.1%}")
        L.append(f"- **语音**：我 {ms['voiceMine']} 条 / 对方 {ms['voiceTheirs']} 条"
                 f"｜**图片**：我 {ms['imageMine']} 张 / 对方 {ms['imageTheirs']} 张")
        L.append("")

        L.append("## 七、关键事件时间线")
        L.append("")
        for x in tl:
            L.append(f"- **{x['month']}**（{x['count']} 条）：{x['firstText']}"
                     + (f"｜峰值日 {x['peakDay']['day']}（{x['peakDay']['count']} 条）"
                        if x.get("peakDay") else ""))
        L.append("")
        L.append("---")
        L.append("*由墨溯 mosu profile 生成｜纯本地统计，无外部调用*")
        return "\n".join(L)

    @staticmethod
    def _dur(sec: int) -> str:
        if sec < 60:
            return f"{sec} 秒"
        if sec < 3600:
            return f"{sec//60} 分 {sec%60} 秒"
        if sec < 86400:
            return f"{sec//3600} 小时 {sec%3600//60} 分"
        return f"{sec//86400} 天 {sec%86400//3600} 小时"
