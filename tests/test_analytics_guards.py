# -*- coding: utf-8 -*-
"""统计与画像的硬约束（遗留风险 #1/#2/#3/#4/#7/#9 + spanDays 统一）。

这些都是"能跑出数字但数字是错的"或"能把服务拖死"的条目，
单测能守住的比例远高于靠人记得：

- 时间分布不补零 → 柱状图在视觉上撒谎（凌晨没消息的小时整段消失）
- 统计不告诉你没去重 → 用户发现"统计比列表多"却找不到原因
- 画像无上限 → 14 万条大群同步持锁，GUI 整体假死且无超时可救
- report 首末消息查了不用 → 白花两次全表扫
- spanDays 一处 int 一处 float → JS 里 3 !== 3.0 为 true，跨页分段判断错乱

用真 SQLite（内存库）而不是 mock：这些 bug 恰恰藏在 SQL 聚合与
零值补齐里，mock 掉就等于没测。
"""
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE {table} (
    local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
    sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER,
    message_content TEXT, packed_info_data BLOB, talker TEXT);
"""


class FakeDataset:
    """最小可用 Dataset：只实现 analytics / profile 真正用到的那几个方法。

    刻意不做加密、不走快照——本轮要验的是聚合口径与上限，
    那些都在 SQL 与循环里，跟解密层无关。

    每个会话一张独立表（与真实分片一致）：analytics 的分片查询**不带会话条件**，
    若所有会话共用一张表，每个会话都会把整库数一遍，统计直接翻倍。
    """

    def __init__(self, sessions, rows, self_name="wxid_me"):
        """sessions: [{sessionId, displayName, type}]
        rows: [(talker, create_ts, local_type, sender_name, content)]"""
        self._sessions = [dict(s) for s in sessions]
        self.user_dir = Path(tempfile.gettempdir()) / "mosu-fake-ds"
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self._con = {}
        self._names = {self_name: 1}
        for _, _, _, sender, _ in rows:
            self._names.setdefault(sender, len(self._names) + 1)
        self._load(rows)
        for i, s in enumerate(self._sessions):
            s["shards"] = [{"shard": "s", "table": f"MSG_{i}"}]

    # ---------------------------------------------------------- 底层
    def _load(self, rows):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE Name2Id (user_name TEXT)")
        con.executemany("INSERT INTO Name2Id (user_name) VALUES (?)",
                        [(n,) for n in self._names])
        tables = {s["sessionId"]: f"MSG_{i}" for i, s in enumerate(self._sessions)}
        for t in set(tables.values()):
            con.executescript(SCHEMA.format(table=t))
        for sid, t in tables.items():
            mine = [r for r in rows if r[0] == sid]
            con.executemany(
                f"INSERT INTO {t} (local_id, server_id, local_type, sort_seq, "
                "real_sender_id, create_time, message_content, packed_info_data, talker) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(i, i, r[2], i, self._names[r[3]], r[1], r[4], None, sid)
                 for i, r in enumerate(mine, 1)])
        con.commit()
        self._con = {"s": con}
        for i, s in enumerate(self._sessions):
            t = f"MSG_{i}"
            s["messageCount"] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            s["sortTime"] = con.execute(
                f"SELECT max(create_time) FROM {t}").fetchone()[0] or 0

    def conn(self, rel):
        return self._con[rel]

    def close(self):
        for c in self._con.values():
            try:
                c.close()
            except Exception:
                pass
        self._con = {}

    # ---------------------------------------------------------- 会话/联系人
    def sessions(self, refresh=False):
        return self._sessions

    def contacts(self):
        return {n: {"displayName": n} for n in self._names}

    def find_session(self, query):
        q = (query or "").strip().lower()
        for s in self._sessions:
            if s["sessionId"].lower() == q or s["displayName"] == query:
                return s
        return None

    def find_sessions(self, query):
        return self.sessions()

    def _shard_list(self, sess):
        return sess.get("shards") or [{"shard": "s", "table": sess["table"]}]

    def self_username(self):
        return next(iter(self._names))

    def self_id(self, shard):
        return self._names[self.self_username()]

    def sender_names(self, shard):
        return dict(self._names)

    # ---------------------------------------------------------- 消息
    def raw_messages(self, sess, limit=50, offset=0, from_ts=None, to_ts=None,
                     direction=None, cursor=None, order="desc"):
        sh = self._shard_list(sess)[0]
        con = self.conn(sh["shard"])
        w, p = [], []
        if from_ts:
            w.append("create_time >= ?"); p.append(from_ts)
        if to_ts:
            w.append("create_time <= ?"); p.append(to_ts)
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = (f"SELECT * FROM {sh['table']}{where} "
               f"ORDER BY create_time {'ASC' if order == 'asc' else 'DESC'} LIMIT ?")
        p.append(int(limit))
        return [(sh["shard"], r) for r in con.execute(sql, p)]

    def iter_session_rows(self, sess, from_ts=None, to_ts=None, desc=False, limit=0):
        sh = self._shard_list(sess)[0]
        con = self.conn(sh["shard"])
        w, p = [], []
        if from_ts:
            w.append("create_time >= ?"); p.append(from_ts)
        if to_ts:
            w.append("create_time <= ?"); p.append(to_ts)
        where = (" WHERE " + " AND ".join(w)) if w else ""
        sql = (f"SELECT * FROM {sh['table']}{where} "
               f"ORDER BY create_time {'DESC' if desc else 'ASC'}")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [(sh["shard"], r) for r in con.execute(sql, p)]

    def to_message(self, row, sess, shard="s"):
        return {"createTime": row["create_time"],
                "kind": "text" if (row["local_type"] & 0xFFFFFFFF) in (1, 49) else "image",
                "content": row["message_content"],
                "direction": "out" if row["real_sender_id"] == self.self_id(shard) else "in",
                "senderName": self._rev().get(row["real_sender_id"], ""),
                "senderId": row["real_sender_id"],
                "imgMd5": None, "voiceMs": None, "title": None,
                "localType": row["local_type"]}

    def _rev(self):
        return {v: k for k, v in self._names.items()}


def _ts(y, m, d, hh=10, mm=0):
    return int(datetime(y, m, d, hh, mm).timestamp())


class _DS(unittest.TestCase):
    """建了 FakeDataset 就得关——否则 unittest 退出时刷一屏 ResourceWarning。"""

    def make(self, sessions, rows, self_name="wxid_me"):
        ds = FakeDataset(sessions, rows, self_name)
        self.addCleanup(ds.close)
        return ds

    def fixture(self):
        """两个会话、跨多小时/多天/多月的少量消息，够验补零与时间范围。"""
        sessions = [
            {"sessionId": "wxid_a", "displayName": "张三", "type": "private"},
            {"sessionId": "wxid_b", "displayName": "李四", "type": "private"},
        ]
        rows = [
            # 张三：2026-03-01 凌晨 2 点（构造"某个小时完全没有消息"）
            ("wxid_a", _ts(2026, 3, 1, 2, 0), 1, "wxid_me", "在吗"),
            ("wxid_a", _ts(2026, 3, 1, 10, 0), 1, "wxid_a", "早啊"),
            ("wxid_a", _ts(2026, 3, 2, 23, 0), 1, "wxid_me", "晚安"),
            # 李四：2026-04-05 周日 15 点（两条都在 15 点，验聚合计数）
            ("wxid_b", _ts(2026, 4, 5, 15, 0), 1, "wxid_me", "周末愉快"),
            ("wxid_b", _ts(2026, 4, 5, 15, 1), 1, "wxid_b", "同乐"),
        ]
        return self.make(sessions, rows)


# ============================================================ #9 时间分布补零
class TestTimeStatsZeroFill(_DS):
    def setUp(self):
        from core.analytics import Analytics
        self.an = Analytics(self.fixture())

    def test_hour_always_24_buckets(self):
        """小时维度必须恒 24 桶。数据只落在 3 个小时，
        不补零的话凌晨那段会在图上凭空消失——那是"没有"而不是"缺失"。"""
        r = self.an.time_stats("hour")
        self.assertEqual(len(r["items"]), 24)
        self.assertEqual(len(r["distribution"]), 24)
        self.assertEqual([x["bucket"] for x in r["items"]],
                         [f"{i:02d}" for i in range(24)])

    def test_hour_zero_filled_buckets_are_zero(self):
        r = self.an.time_stats("hour")
        by = {x["bucket"]: x["count"] for x in r["items"]}
        self.assertEqual(by["02"], 1)      # 张三凌晨 2 点
        self.assertEqual(by["10"], 1)      # 张三上午 10 点
        self.assertEqual(by["15"], 2)      # 李四 15:00 与 15:01
        for h in ("00", "01", "03", "05", "13", "20"):
            self.assertEqual(by[h], 0, f"{h} 点应补零")

    def test_weekday_always_7_buckets(self):
        r = self.an.time_stats("weekday")
        self.assertEqual(len(r["items"]), 7)
        self.assertEqual([x["bucket"] for x in r["items"]],
                         ["周日", "周一", "周二", "周三", "周四", "周五", "周六"])

    def test_weekday_keeps_key_field(self):
        """补零不能把 key 丢掉——前端按 key 取值。"""
        r = self.an.time_stats("weekday")
        self.assertEqual([x["key"] for x in r["items"]], [str(i) for i in range(7)])

    def test_zero_fill_does_not_inflate_total(self):
        """补零只影响展示，总数必须是真实消息数。"""
        r = self.an.time_stats("hour")
        self.assertEqual(r["total"], 5)
        self.assertEqual(sum(x["count"] for x in r["items"]), 5)

    def test_month_is_not_zero_filled(self):
        """month 补零会产生大量空月份＝噪声，明确不补。"""
        r = self.an.time_stats("month")
        self.assertEqual(len(r["items"]), 2)      # 只有 2026-03 / 2026-04
        self.assertEqual({x["bucket"] for x in r["items"]}, {"2026-03", "2026-04"})

    def test_date_is_not_zero_filled(self):
        r = self.an.time_stats("date")
        self.assertEqual(len(r["items"]), 3)      # 03-01 / 03-02 / 04-05

    def test_empty_db_still_returns_full_hour_axis(self):
        """空库也要 24 桶，否则前端图列数会跳。"""
        from core.analytics import Analytics
        an = Analytics(self.make([{"sessionId": "wxid_x", "displayName": "空"}], []))
        r = an.time_stats("hour")
        self.assertEqual(len(r["items"]), 24)
        self.assertEqual(r["total"], 0)

    def test_fixed_buckets_table_matches_reality(self):
        """FIXED_BUCKETS 是补零桶数的唯一来源——改了表忘了改逻辑，
        或者逻辑写死 24 而表还写别的，都会让文档与实现对不上。"""
        from core.analytics import Analytics
        self.assertEqual(Analytics.FIXED_BUCKETS, {"hour": 24, "weekday": 7})
        self.assertEqual(len(self.an.time_stats("hour")["items"]),
                         Analytics.FIXED_BUCKETS["hour"])
        self.assertEqual(len(self.an.time_stats("weekday")["items"]),
                         Analytics.FIXED_BUCKETS["weekday"])
        # month / date 不在表里＝不补零
        for by in ("month", "date"):
            self.assertNotIn(by, Analytics.FIXED_BUCKETS)


# ============================================================ #2 统计时间范围
class TestStatsTimeRange(_DS):
    def setUp(self):
        from core.analytics import Analytics
        self.an = Analytics(self.fixture())

    def test_global_stats_all_time_is_full_range(self):
        """缺省＝全量，既有行为不变。"""
        self.assertEqual(self.an.global_stats()["totalMessages"], 5)

    def test_global_stats_respects_from(self):
        r = self.an.global_stats(from_ts=_ts(2026, 4, 1))
        self.assertEqual(r["totalMessages"], 2)      # 只剩李四那两条

    def test_global_stats_respects_to(self):
        r = self.an.global_stats(to_ts=_ts(2026, 3, 31))
        self.assertEqual(r["totalMessages"], 3)      # 只剩张三那三条

    def test_global_stats_range_union(self):
        r = self.an.global_stats(from_ts=_ts(2026, 3, 2),
                                 to_ts=_ts(2026, 4, 5, 23, 59))
        self.assertEqual(r["totalMessages"], 3)      # 03-02 一条 + 04-05 两条

    def test_contact_stats_respects_range(self):
        r = self.an.contact_stats(from_ts=_ts(2026, 4, 1))
        self.assertEqual([c["wxid"] for c in r["contacts"]], ["wxid_b"])

    def test_time_stats_respects_range(self):
        r = self.an.time_stats("hour", from_ts=_ts(2026, 4, 1))
        self.assertEqual(r["total"], 2)

    def test_time_range_is_orthogonal_to_by(self):
        """from/to 与 by 互不影响，两条路径都要通。"""
        a = self.an.time_stats("month", from_ts=_ts(2026, 4, 1))
        b = self.an.time_stats("hour", from_ts=_ts(2026, 4, 1))
        self.assertEqual(a["total"], 2)
        self.assertEqual(b["total"], 2)


# ============================================================ #1 口径透明
class TestDedupedFlag(_DS):
    def setUp(self):
        from core.analytics import Analytics
        self.an = Analytics(self.fixture())

    def test_global_stats_declares_not_deduped(self):
        r = self.an.global_stats()
        self.assertIn("deduped", r)
        self.assertIs(r["deduped"], False)

    def test_session_stats_declares_not_deduped(self):
        r = self.an.session_stats("wxid_a")
        self.assertIs(r["deduped"], False)

    def test_flag_present_even_when_range_given(self):
        self.assertIs(self.an.global_stats(from_ts=_ts(2026, 4, 1))["deduped"], False)


# ============================================================ #7 report 首末消息
class TestReportFirstLast(_DS):
    def setUp(self):
        from core.analytics import Analytics
        self.an = Analytics(self.fixture())

    def test_session_report_has_first_and_last(self):
        r = self.an.report(session="wxid_a")
        s = r["summary"]
        self.assertIn("firstMessage", s)
        self.assertIn("lastMessage", s)
        self.assertEqual(s["firstMessage"]["content"], "在吗")
        self.assertEqual(s["lastMessage"]["content"], "晚安")

    def test_first_last_content_not_empty(self):
        """这两条查出来就是要给人看的——content 为空等于白查。"""
        s = self.an.report(session="wxid_a")["summary"]
        for k in ("firstMessage", "lastMessage"):
            self.assertTrue(s[k]["content"], f"{k}.content 不应为空")
            self.assertIsInstance(s[k]["time"], int)

    def test_first_precedes_last(self):
        s = self.an.report(session="wxid_a")["summary"]
        self.assertLess(s["firstMessage"]["time"], s["lastMessage"]["time"])

    def test_non_session_report_is_null(self):
        """非会话口径没有"对方视角"，给 null 而不是全库首条（无意义）。"""
        s = self.an.report(all_time=True)["summary"]
        self.assertIsNone(s["firstMessage"])
        self.assertIsNone(s["lastMessage"])

    def test_report_keeps_existing_time_fields(self):
        """加字段不能碰坏既有字段——firstMessageTime 仍在。"""
        s = self.an.report(session="wxid_a")["summary"]
        self.assertEqual(s["firstMessageTime"], _ts(2026, 3, 1, 2, 0))
        self.assertEqual(s["lastMessageTime"], _ts(2026, 3, 2, 23, 0))


# ============================================================ #3 画像上限
class TestContactProfileCap(_DS):
    def _pd(self, n):
        from core.profile import ProfileDistiller
        rows = [("wxid_a", _ts(2026, 3, 1, 0, i % 60), 1,
                 "wxid_me" if i % 2 else "wxid_a", f"消息{i}") for i in range(n)]
        return ProfileDistiller(self.make(
            [{"sessionId": "wxid_a", "displayName": "张三", "type": "private"}], rows))

    def test_default_cap_is_60000(self):
        from core.profile import ProfileDistiller
        self.assertEqual(ProfileDistiller.CONTACT_MAX_MSGS, 60000)

    def test_not_truncated_under_cap(self):
        d = self._pd(10).contact_profile("wxid_a")
        self.assertIs(d["overview"]["truncated"], False)
        self.assertEqual(d["meta"]["scannedMessages"], 10)

    def test_truncated_when_hitting_cap(self):
        d = self._pd(50).contact_profile("wxid_a", max_msgs=20)
        self.assertIs(d["overview"]["truncated"], True)
        self.assertEqual(d["meta"]["scannedMessages"], 20)
        self.assertEqual(d["meta"]["maxMsgs"], 20)

    def test_exactly_at_cap_is_not_truncated(self):
        """正好等于上限不算截断——多取的那一条才是判据。"""
        d = self._pd(20).contact_profile("wxid_a", max_msgs=20)
        self.assertIs(d["overview"]["truncated"], False)

    def test_one_over_cap_is_truncated(self):
        d = self._pd(21).contact_profile("wxid_a", max_msgs=20)
        self.assertIs(d["overview"]["truncated"], True)
        self.assertEqual(d["meta"]["scannedMessages"], 20)

    def test_truncated_flag_in_both_places(self):
        """前端读 overview.more 之外的路径也要能拿到——两处都写。"""
        d = self._pd(50).contact_profile("wxid_a", max_msgs=20)
        self.assertIs(d["meta"]["truncated"], True)
        self.assertIs(d["overview"]["truncated"], True)

    def test_default_cap_actually_applied(self):
        """不传 max_msgs 时也要走 CONTACT_MAX_MSGS，不是无限。"""
        d = self._pd(30).contact_profile("wxid_a")
        self.assertEqual(d["meta"]["maxMsgs"], 60000)
        self.assertIs(d["overview"]["truncated"], False)


# ============================================================ spanDays 类型统一
class TestSpanDaysType(_DS):
    """JS 里 3 !== 3.0 为 true。两处类型不一致会让跨页分段判断错乱。"""

    def test_contact_span_days_is_float(self):
        from core.profile import ProfileDistiller
        rows = [("wxid_a", _ts(2026, 3, 1), 1, "wxid_me", "a"),
                ("wxid_a", _ts(2026, 3, 4), 1, "wxid_a", "b")]     # 恰好 3 天
        ds = self.make([{"sessionId": "wxid_a", "displayName": "张三",
                         "type": "private"}], rows)
        d = ProfileDistiller(ds).contact_profile("wxid_a")
        self.assertIsInstance(d["overview"]["spanDays"], float)
        self.assertEqual(d["overview"]["spanDays"], 3.0)

    def test_self_span_days_is_float(self):
        from core.profile import ProfileDistiller
        rows = [("wxid_a", _ts(2026, 3, 1), 1, "wxid_me", "a"),
                ("wxid_a", _ts(2026, 3, 4), 1, "wxid_me", "b")]
        ds = self.make([{"sessionId": "wxid_a", "displayName": "张三",
                         "type": "private"}], rows)
        d = ProfileDistiller(ds).self_profile()
        self.assertIsInstance(d["overview"]["spanDays"], float)
        self.assertEqual(d["overview"]["spanDays"], 3.0)

    def test_both_scopes_agree_on_same_span(self):
        """同一段对话两处口径必须给出相同的 float 值。"""
        from core.profile import ProfileDistiller
        rows = [("wxid_a", _ts(2026, 3, 1), 1, "wxid_me", "a"),
                ("wxid_a", _ts(2026, 3, 4), 1, "wxid_me", "b")]
        ds = self.make([{"sessionId": "wxid_a", "displayName": "张三",
                         "type": "private"}], rows)
        pd = ProfileDistiller(ds)
        a = pd.self_profile()["overview"]["spanDays"]
        b = pd.contact_profile("wxid_a")["overview"]["spanDays"]
        self.assertEqual(a, b)
        self.assertIs(type(a), type(b))


# ============================================================ #3 画像异步化
class TestProfileAsyncPath(unittest.TestCase):
    """`/api/profile?async=1` 走 TaskHub；不带 async 仍是同步旧路径。

    一次只切一半是有意的：CLI / MCP 间接依赖 ProfileDistiller 的同步语义，
    一起切爆炸半径不值。所以两条路径必须同时在。
    """

    def _src(self):
        import inspect
        import gui.server as S
        return inspect.getsource(S.run_gui)

    def test_async_flag_supported(self):
        self.assertIn('q.get("async")', self._src())

    def test_async_submits_profile_kind_task(self):
        """kind="profile" 是新增取值——前端按 kind 分流处要认这一支。"""
        src = self._src()
        self.assertIn('tasks.submit("profile"', src)

    def test_async_returns_taskid_envelope(self):
        src = self._src()
        self.assertIn('return {"taskId": tid, "status": "running"}', src)

    def test_sync_path_preserved(self):
        """不带 async 仍走同步——不能一刀切掉旧路径。"""
        src = self._src()
        self.assertIn("pd.contact_profile(sess, from_ts=from_ts, to_ts=to_ts)", src)
        self.assertIn("pd.self_profile(from_ts=from_ts, to_ts=to_ts)", src)

    def test_profile_task_takes_lock_inside_worker(self):
        """锁必须在 worker 内拿，而不是提交时就拿——否则异步等于白做。"""
        import inspect
        import gui.server as S
        src = inspect.getsource(S.run_gui)
        i = src.index("def _profile_task(")
        body = src[i:src.index("def api_profile(", i)]
        self.assertIn("with _ds_lock:", body)
        self.assertLess(body.index("def run(tid)"), body.index("with _ds_lock:"))

    def test_profile_cap_applied_on_async_path_too(self):
        """异步路径也必须受 CONTACT_MAX_MSGS 保护，不能只保护同步那条。"""
        src = self._src()
        self.assertIn("def _profile_task(", src)
        body = src[src.index("def _profile_task("):src.index("def api_profile(")]
        self.assertNotIn("max_msgs=10 ** 9", body)
        self.assertNotIn("limit=10 ** 9", body)


# ============================================================ #2 统计接口接线
class TestStatsEndpointWiring(unittest.TestCase):
    """三个统计端点都要接上 from/to，且缺省＝全量（既有行为不变）。"""

    def _src(self):
        import inspect
        import gui.server as S
        return inspect.getsource(S.run_gui)

    def test_shared_range_helper_exists(self):
        self.assertIn("def _range_ts(q)", self._src())

    def test_global_accepts_range(self):
        src = self._src()
        i = src.index("def api_stats_global(")
        body = src[i:src.index("def api_stats_contacts(", i)]
        self.assertIn("_range_ts(q)", body)
        self.assertIn("global_stats(from_ts, to_ts)", body)

    def test_contacts_accepts_range(self):
        src = self._src()
        i = src.index("def api_stats_contacts(")
        body = src[i:src.index("def api_stats_time(", i)]
        self.assertIn("_range_ts(q)", body)
        self.assertIn("contact_stats(top=top, from_ts=from_ts, to_ts=to_ts)", body)

    def test_time_accepts_range(self):
        src = self._src()
        i = src.index("def api_stats_time(")
        body = src[i:src.index("def _kw_task(", i)]
        self.assertIn("time_stats(by=by, from_ts=from_ts, to_ts=to_ts)", body)

    def test_global_route_passes_params(self):
        """/api/stats/global 原来不传参——不改这里 from/to 永远收不到。"""
        src = self._src()
        self.assertIn("self._guard(api_stats_global, self._params())", src)

    def test_parse_time_is_reused(self):
        """复用 core.dataset.parse_time，不要另写一套日期解析。"""
        self.assertIn("from core.dataset import parse_time", self._src())


# ============================================================ #4 关键词上限
class TestKeywordLimits(unittest.TestCase):
    def test_server_cap_is_20000(self):
        """TaskHub worker 全程持锁，20 万条＝GUI 十几分钟全僵。"""
        import inspect
        import gui.server as S
        src = inspect.getsource(S.run_gui)
        self.assertIn("max_msgs 需在 1000~20000", src)
        self.assertNotIn("1000~200000", src)

    def test_999999_rejected(self):
        import inspect
        import gui.server as S
        # 真跑一次解析逻辑代价太大，直接验边界常量与比较式
        src = inspect.getsource(S.run_gui)
        self.assertIn("1000 <= max_msgs <= 20000", src)

    def test_default_max_msgs_is_the_cap(self):
        from core.analytics import Analytics
        self.assertEqual(inspect_default(Analytics.keyword_stats), 20000)
        self.assertEqual(inspect_default(Analytics._texts), 20000)

    def test_texts_yields_periodically(self):
        """逐条解压是纯 Python 计算，不让出 GIL 会饿死持锁的主线程。"""
        import inspect
        from core.analytics import Analytics
        self.assertEqual(Analytics.YIELD_EVERY, 5000)
        src = inspect.getsource(Analytics._texts)
        self.assertIn("time.sleep(0)", src)
        self.assertIn("% self.YIELD_EVERY == 0", src)


def inspect_default(fn):
    import inspect
    return inspect.signature(fn).parameters["max_msgs"].default


if __name__ == "__main__":
    unittest.main()
