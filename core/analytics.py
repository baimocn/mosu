# -*- coding: utf-8 -*-
"""
墨溯 core —— 统计与年度报告（尽量用 SQL 聚合，避免逐条解压）

对外:
    Analytics(ds).global_stats() / contacts() / time() / session() /
                keywords() / group() / report()
"""
from __future__ import annotations

import re
import time
from collections import Counter
from datetime import datetime

from core.dataset import KIND_BY_TYPE
from core.msgparse import parse_message

STOPWORDS = set("""
的 了 是 在 我 你 他 她 它 我们 你们 他们 这 那 就 都 也 和 与 有 没 不 吧 啊 哦 呢 吗 什么 怎么
一个 可以 现在 时候 知道 因为 所以 但是 然后 如果 还是 这个 那个 觉得 感觉 有点 一下 今天 明天 昨天
就是 不是 没有 自己 已经 还有 这样 那样 怎么 干嘛 哈哈 嘿嘿 嗯 哦 额 好 对 行 好的 谢谢 没事
""".split())

_WORD_RE = re.compile(r"[一-龥A-Za-z0-9]{2,}")
_ID_RE = re.compile(r"(?=.*[0-9])(?=.*[a-zA-Z])[0-9a-zA-Z_.-]{6,}")


def clean_text(t: str | None) -> str:
    """清洗消息文本：表情/引用/卡片等消息体是 XML 或内部标识串，没有自然语言。

    否则 `<emoji .../>` 的属性名、`com.tencent.xin.emoticon.person.stiker_1728...`
    这类 productid 会污染词频统计。
    """
    if not t:
        return ""
    s = t.strip()
    if s.startswith("<") and ">" in s:
        return ""
    if len(s) >= 8 and not any("\u4e00" <= c <= "\u9fff" for c in s) and ("." in s or "_" in s):
        return ""          # com.tencent.xin... / xxx_17280338773 之类的内部 id
    # 微信表情在纯文本里长这样：[旺柴] [捂脸] [疑问] —— 不是自然语言
    s = re.sub(r"\[[\u4e00-\u9fff]{1,8}\]", "", s)
    return s.strip()


def extract_keywords(texts, top: int = 30) -> list[tuple[str, int]]:
    """中文分词统计（jieba 可用时优先，否则退化为 2-gram）。"""
    freq: Counter = Counter()
    try:
        import logging
        import warnings
        logging.getLogger("jieba").setLevel(logging.ERROR)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import jieba
        use_jieba = True
    except Exception:
        use_jieba = False
    for t in texts:
        if not t:
            continue
        t = clean_text(t)
        if not t:
            continue
        if use_jieba:
            words = [w.strip() for w in jieba.cut(t)]
        else:
            words = _WORD_RE.findall(t)
        for w in words:
            if len(w) < 2 or w in STOPWORDS or w.isdigit():
                continue
            if w.startswith("wxid") or w.startswith("gh_"):
                continue
            if _ID_RE.fullmatch(w):          # 微信号 / 群号之类的 id 噪音
                continue
            freq[w] += 1
    return freq.most_common(top)


class Analytics:
    def __init__(self, ds):
        self.ds = ds

    # ------------------------------------------------------------ 内部工具
    def _where(self, from_ts=None, to_ts=None) -> tuple[str, list]:
        w, p = [], []
        if from_ts:
            w.append("create_time >= ?")
            p.append(from_ts)
        if to_ts:
            w.append("create_time <= ?")
            p.append(to_ts)
        return (" WHERE " + " AND ".join(w) if w else ""), p

    def _shards(self, session=None) -> list[tuple[dict, dict]]:
        """[(session, shard)]"""
        out = []
        if session:
            s = self.ds.find_session(session)
            if not s:
                raise SystemExit(f"[x] 没找到会话「{session}」")
            for sh in self.ds._shard_list(s):
                out.append((s, sh))
        else:
            for s in self.ds.sessions():
                for sh in self.ds._shard_list(s):
                    out.append((s, sh))
        return out

    def _kind_count(self, shard: dict, from_ts=None, to_ts=None) -> dict:
        con = self.ds.conn(shard["shard"])
        where, params = self._where(from_ts, to_ts)
        sql = f'SELECT local_type, count(*) c FROM "{shard["table"]}"{where} GROUP BY local_type'
        out = {}
        for r in con.execute(sql, params):
            k = KIND_BY_TYPE.get(r[0] & 0xFFFFFFFF, "other")
            out[k] = out.get(k, 0) + r[1]
        return out

    # ------------------------------------------------------------ 全局
    def global_stats(self, from_ts=None, to_ts=None) -> dict:
        total = 0
        kinds: Counter = Counter()
        first = last = None
        for sess, sh in self._shards():
            con = self.ds.conn(sh["shard"])
            where, params = self._where(from_ts, to_ts)
            r = con.execute(f'SELECT count(*), min(create_time), max(create_time) '
                            f'FROM "{sh["table"]}"{where}', params).fetchone()
            total += r[0] or 0
            if r[1]:
                first = r[1] if first is None else min(first, r[1])
            if r[2]:
                last = r[2] if last is None else max(last, r[2])
            for k, v in self._kind_count(sh, from_ts, to_ts).items():
                kinds[k] += v
        media = sum(v for k, v in kinds.items() if k != "text")
        return {
            "totalMessages": total,
            "totalSessions": len(self.ds.sessions()),
            "totalContacts": len(self.ds.contacts()),
            "textMessages": kinds.get("text", 0),
            "mediaMessages": media,
            "byType": dict(kinds.most_common()),
            "timeRange": {"first": first, "last": last,
                          "firstText": datetime.fromtimestamp(first).strftime("%Y-%m-%d") if first else None,
                          "lastText": datetime.fromtimestamp(last).strftime("%Y-%m-%d") if last else None},
        }

    # ------------------------------------------------------------ 联系人榜
    def contact_stats(self, top: int = 20, from_ts=None, to_ts=None) -> dict:
        rows = []
        for sess, sh in self._shards():
            con = self.ds.conn(sh["shard"])
            where, params = self._where(from_ts, to_ts)
            r = con.execute(f'SELECT count(*) FROM "{sh["table"]}"{where}', params).fetchone()
            n = r[0] or 0
            if n:
                rows.append({"wxid": sess["sessionId"], "displayName": sess["displayName"],
                             "type": sess["type"], "messageCount": n})
        agg: dict[str, dict] = {}
        for r in rows:
            k = r["wxid"]
            if k in agg:
                agg[k]["messageCount"] += r["messageCount"]
            else:
                agg[k] = r
        out = sorted(agg.values(), key=lambda x: -x["messageCount"])[:top]
        return {"contacts": out, "total": len(agg)}

    # ------------------------------------------------------------ 时间分布
    def time_stats(self, by: str = "month", session=None, from_ts=None, to_ts=None) -> dict:
        expr = {"hour": "%H", "weekday": "%w", "month": "%Y-%m", "date": "%Y-%m-%d"}.get(by, "%Y-%m")
        dist: Counter = Counter()
        for sess, sh in self._shards(session):
            con = self.ds.conn(sh["shard"])
            where, params = self._where(from_ts, to_ts)
            sql = (f"SELECT strftime('{expr}', create_time, 'unixepoch', 'localtime') k, count(*) "
                   f'FROM "{sh["table"]}"{where} GROUP BY k')
            for r in con.execute(sql, params):
                dist[r[0]] += r[1]
        keys = sorted(dist.keys())
        if by == "weekday":
            names = {"0": "周日", "1": "周一", "2": "周二", "3": "周三",
                     "4": "周四", "5": "周五", "6": "周六"}
            items = [{"bucket": names.get(k, k), "key": k, "count": dist[k]} for k in keys]
        else:
            items = [{"bucket": k, "count": dist[k]} for k in keys]
        return {"by": by, "distribution": dict(dist), "items": items,
                "total": sum(dist.values())}

    # ------------------------------------------------------------ 单会话
    def session_stats(self, session: str, from_ts=None, to_ts=None) -> dict:
        sess = self.ds.find_session(session)
        if not sess:
            raise SystemExit(f"[x] 没找到会话「{session}」")
        total = sent = recv = 0
        first = last = None
        days: set = set()
        kinds: Counter = Counter()
        for sh in self.ds._shard_list(sess):
            con = self.ds.conn(sh["shard"])
            where, params = self._where(from_ts, to_ts)
            r = con.execute(f'SELECT count(*), min(create_time), max(create_time) '
                            f'FROM "{sh["table"]}"{where}', params).fetchone()
            total += r[0] or 0
            if r[1]:
                first = r[1] if first is None else min(first, r[1])
            if r[2]:
                last = r[2] if last is None else max(last, r[2])
            sid = self.ds.self_id(sh["shard"])
            where2, p2 = self._where(from_ts, to_ts)
            where2 = where2 + (" AND " if where2 else " WHERE ") + "real_sender_id = ?"
            p2 = p2 + [sid]
            sql = f'SELECT count(*) FROM "{sh["table"]}"{where2}'
            sent += con.execute(sql, p2).fetchone()[0] or 0
            for k, v in self._kind_count(sh, from_ts, to_ts).items():
                kinds[k] += v
            sql = (f"SELECT DISTINCT strftime('%Y-%m-%d', create_time, 'unixepoch', 'localtime') "
                   f'FROM "{sh["table"]}"{where}')
            for r in con.execute(sql, params):
                days.add(r[0])
        recv = max(0, total - sent)
        return {
            "session": {"sessionId": sess["sessionId"], "displayName": sess["displayName"],
                        "type": sess["type"]},
            "totalMessages": total,
            "textMessages": kinds.get("text", 0),
            "mediaMessages": sum(v for k, v in kinds.items() if k != "text"),
            "byType": dict(kinds.most_common()),
            "sentMessages": sent,
            "receivedMessages": recv,
            "activeDays": len(days),
            "firstMessageTime": first,
            "lastMessageTime": last,
            "firstMessageText": datetime.fromtimestamp(first).strftime("%Y-%m-%d %H:%M") if first else None,
            "lastMessageText": datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M") if last else None,
        }

    # ------------------------------------------------------------ 群聊
    def group_stats(self, group: str, from_ts=None, to_ts=None, top: int = 20) -> dict:
        sess = self.ds.find_session(group)
        if not sess:
            raise SystemExit(f"[x] 没找到会话「{group}」")
        members: Counter = Counter()
        total = 0
        for sh in self.ds._shard_list(sess):
            con = self.ds.conn(sh["shard"])
            where, params = self._where(from_ts, to_ts)
            sql = (f'SELECT real_sender_id, count(*) c FROM "{sh["table"]}"{where} '
                   f'GROUP BY real_sender_id')
            names = self.ds.sender_names(sh["shard"])
            for r in con.execute(sql, params):
                members[r[0]] += r[1]
        total = sum(members.values())
        sid = self.ds.self_id(self.ds._shard_list(sess)[0]["shard"])
        names = self.ds.sender_names(self.ds._shard_list(sess)[0]["shard"])
        top_members = [{"senderId": k, "displayName": names.get(k) or f"#{k}",
                        "messageCount": v, "isSelf": k == sid}
                       for k, v in members.most_common(top)]
        return {
            "session": {"sessionId": sess["sessionId"], "displayName": sess["displayName"]},
            "totalMessages": total,
            "activeMembers": len(members),
            "topMembers": top_members,
        }

    # ------------------------------------------------------------ 关键词
    def _texts(self, session=None, from_ts=None, to_ts=None, max_msgs: int = 20000,
               recent: bool = True) -> list[str]:
        """抽取文本内容（需要逐条解压，max_msgs 控制上限）。"""
        texts: list[str] = []
        n = 0
        targets = ([self.ds.find_session(session)] if session else self.ds.sessions())
        targets = [t for t in targets if t]
        if recent:
            targets = sorted(targets, key=lambda s: s.get("sortTime") or s.get("lastTime") or 0,
                             reverse=True)
        for sess in targets:
            for shard, r in self.ds.iter_session_rows(sess, from_ts, to_ts):
                if n >= max_msgs:
                    return texts
                m = parse_message(r["local_type"], r["message_content"], r["packed_info_data"])
                t = (m.get("text") or "").strip()
                if t and (m["type"] & 0xFFFFFFFF) in (1, 49):
                    texts.append(t)
                n += 1
        return texts

    def keyword_stats(self, session=None, top: int = 30, from_ts=None, to_ts=None,
                      max_msgs: int = 20000) -> dict:
        t0 = time.time()
        texts = self._texts(session, from_ts, to_ts, max_msgs)
        kw = extract_keywords(texts, top)
        return {"keywords": [{"word": w, "count": c} for w, c in kw],
                "scannedMessages": len(texts), "cost": round(time.time() - t0, 2),
                "sessionId": self.ds.find_session(session)["sessionId"] if session else None}

    # ------------------------------------------------------------ 年度报告
    def report(self, year: int | None = None, all_time: bool = False, session=None,
               top_contacts: int = 10, top_keywords: int = 20,
               max_msgs: int = 30000) -> dict:
        now = datetime.now()
        if all_time:
            from_ts = to_ts = None
            scope, y = "all", None
        elif session:
            from_ts = to_ts = None
            scope, y = "session", year
        else:
            y = year or now.year
            from_ts = int(datetime(y, 1, 1).timestamp())
            to_ts = int(datetime(y + 1, 1, 1).timestamp())
            scope, y = "year", y

        if session:
            st = self.session_stats(session, from_ts, to_ts)
            total = st["totalMessages"]
            sent, recv = st["sentMessages"], st["receivedMessages"]
            days = st["activeDays"]
            first, last = st["firstMessageTime"], st["lastMessageTime"]
            td = self.time_stats("hour", session=session, from_ts=from_ts, to_ts=to_ts)
            contacts = [{"wxid": st["session"]["sessionId"],
                         "displayName": st["session"]["displayName"], "count": total}]
        else:
            g = self.global_stats(from_ts, to_ts)
            total = g["totalMessages"]
            cs = self.contact_stats(top_contacts, from_ts, to_ts)
            contacts = [{"wxid": c["wxid"], "displayName": c["displayName"],
                         "count": c["messageCount"]} for c in cs["contacts"]]
            sent = recv = 0
            for sess, sh in self._shards():
                con = self.ds.conn(sh["shard"])
                where, params = self._where(from_ts, to_ts)
                sid = self.ds.self_id(sh["shard"])
                w2 = where + (" AND " if where else " WHERE ") + "real_sender_id = ?"
                sent += con.execute(f'SELECT count(*) FROM "{sh["table"]}"{w2}',
                                    params + [sid]).fetchone()[0] or 0
            recv = max(0, total - sent)
            days_set: set = set()
            for sess, sh in self._shards():
                con = self.ds.conn(sh["shard"])
                where, params = self._where(from_ts, to_ts)
                sql = (f"SELECT DISTINCT strftime('%Y-%m-%d', create_time, 'unixepoch','localtime') "
                       f'FROM "{sh["table"]}"{where}')
                for r in con.execute(sql, params):
                    days_set.add(r[0])
            days = len(days_set)
            first, last = g["timeRange"]["first"], g["timeRange"]["last"]
            td = self.time_stats("hour", from_ts=from_ts, to_ts=to_ts)

        kw = self.keyword_stats(session, top_keywords, from_ts, to_ts, max_msgs)
        first_msg = last_msg = None
        if session:
            sess = self.ds.find_session(session)
            rows = self.ds.raw_messages(sess, limit=1, order="asc", from_ts=from_ts, to_ts=to_ts)
            if rows:
                m = self.ds.to_message(rows[0][1], sess, shard=rows[0][0])
                first_msg = {"time": m["createTime"], "content": (m["content"] or "")[:80]}
            rows = self.ds.raw_messages(sess, limit=1, order="desc", from_ts=from_ts, to_ts=to_ts)
            if rows:
                m = self.ds.to_message(rows[0][1], sess, shard=rows[0][0])
                last_msg = {"time": m["createTime"], "content": (m["content"] or "")[:80]}

        return {
            "scope": scope,
            "year": y,
            "sessionId": self.ds.find_session(session)["sessionId"] if session else None,
            "summary": {
                "totalMessages": total,
                "sentMessages": sent,
                "receivedMessages": recv,
                "activeDays": days,
                "topContacts": contacts,
                "topKeywords": kw["keywords"],
                "hourlyDistribution": td["distribution"],
                "firstMessageTime": first,
                "lastMessageTime": last,
            },
            "meta": {"scannedMessages": kw["scannedMessages"], "cost": kw["cost"]},
        }
