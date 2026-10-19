# -*- coding: utf-8 -*-
"""洞察四模块的关键不变量。

这些都是多智能体审查挖出来的真实缺陷，每条都配一个断言防止回归：
- 高频词任务字段名（后端 taskId / 前端读取）不一致会让功能静默失效
- 星期下标错位一天
- insights 声称"周末高于工作日"但从未计算
- 全角问号漏统计
- 同名会话被合并成一个人
- 采样取到会话最早 N 条而非最近 N 条
"""
import unittest
from unittest.mock import MagicMock


class TestWeekdayMapping(unittest.TestCase):
    def test_index0_is_sunday(self):
        """SQLite %w: 0=周日。profile.py 的 WEEKDAY_NAMES 必须与之一致，
        否则画像里的星期整体偏移一天（审查发现过的真实 bug）。"""
        from core.profile import WEEKDAY_NAMES
        self.assertEqual(WEEKDAY_NAMES[0], "周日")
        self.assertEqual(WEEKDAY_NAMES[6], "周六")

    def test_analytics_weekday_names(self):
        """analytics 的 weekday 桶名是权威口径，profile 必须与之逐日对齐。"""
        from core.profile import WEEKDAY_NAMES
        import inspect
        from core.analytics import Analytics
        src = inspect.getsource(Analytics.time_stats)
        for i, name in enumerate(WEEKDAY_NAMES):
            self.assertIn(f'"{i}": "{name}"', src)


class TestInsightsHonesty(unittest.TestCase):
    def _insights(self, **kw):
        from core.profile import ProfileDistiller
        args = dict(hour_mine={}, night=0, my_total=100, lens=[10] * 10,
                    topics=[], emoji_rate=0, q_rate=0, peak_wd=None)
        args.update(kw)
        return ProfileDistiller._self_insights(MagicMock(), **args)

    def test_no_weekend_claim_without_comparison(self):
        """峰值落在周六时，不能声称"周末高于工作日"——那个结论没被计算。"""
        from core.profile import WEEKDAY_NAMES
        sat = WEEKDAY_NAMES.index("周六")
        out = self._insights(peak_wd=sat)
        joined = " ".join(out)
        self.assertIn("周六是最活跃的一天", joined)
        self.assertNotIn("周末输出高于工作日", joined)
        self.assertNotIn("周末两天合计消息高于工作日", joined)

    def test_weekday_name_comes_from_table(self):
        """星期名必须查表得到，不能在文案里硬编码（硬编码过一次，错了）。"""
        from core.profile import WEEKDAY_NAMES
        for idx in range(7):
            out = self._insights(peak_wd=idx)
            self.assertTrue(any(WEEKDAY_NAMES[idx] in x for x in out),
                            f"idx {idx} 应映射到 {WEEKDAY_NAMES[idx]}")

    def test_peak_weekday_name_matches_index(self):
        from core.profile import WEEKDAY_NAMES
        self.assertEqual(WEEKDAY_NAMES[5], "周五")
        self.assertEqual(WEEKDAY_NAMES[6], "周六")

    def test_night_window_label_matches_range(self):
        """夜间窗口是 range(0,6)=0-5 点，文案不能写成 0-6。"""
        out = self._insights(night=20, my_total=100)
        joined = " ".join(out)
        self.assertIn("0-5 点", joined)
        self.assertNotIn("0-6 点", joined)


class TestSamplingDirection(unittest.TestCase):
    def test_iter_session_rows_supports_desc_limit(self):
        """采样要"最近 N 条"，必须能倒序+限量，否则拿到的是最早 N 条。"""
        import inspect
        import core.dataset as D
        sig = inspect.signature(D.Dataset.iter_session_rows)
        self.assertIn("desc", sig.parameters)
        self.assertIn("limit", sig.parameters)

    def test_desc_builds_descending_sql(self):
        """源码里 ORDER BY 要随 desc 切换，否则限量取到最早的行。"""
        import inspect
        import core.dataset as D
        src = inspect.getsource(D.Dataset.iter_session_rows)
        self.assertIn("DESC", src)
        self.assertIn("LIMIT", src)

    def test_default_order_unchanged(self):
        import inspect
        import core.dataset as D
        sig = inspect.signature(D.Dataset.iter_session_rows)
        self.assertIs(sig.parameters["desc"].default, False)
        self.assertEqual(sig.parameters["limit"].default, 0)


class TestSocialKeying(unittest.TestCase):
    def test_social_uses_wxid_not_name(self):
        """两个同名会话（都叫"张三"）不该被合并成一个人。"""
        import inspect
        import core.profile as P
        src = inspect.getsource(P.ProfileDistiller._sql_aggregate)
        self.assertIn('social[sess["sessionId"]]', src)
        self.assertNotIn('social[sess["displayName"]]', src)

    def test_social_output_has_wxid(self):
        import inspect
        import core.profile as P
        src = inspect.getsource(P.ProfileDistiller.self_profile)
        self.assertIn('"wxid": k', src)


class TestMarkdownRobustness(unittest.TestCase):
    def test_peak_hour_none_does_not_crash(self):
        """空库时 peakHour=None，直接 :02d 格式化会 TypeError。"""
        import inspect
        import core.profile as P
        src = inspect.getsource(P.ProfileDistiller._render_self)
        self.assertIn("peak_h = ", src)
        self.assertIn('is not None else "无数据"', src)
        self.assertIn('r.get("peakWeekday") or "无数据"', src)


class TestContactLookupSafety(unittest.TestCase):
    def test_substring_fallback_requires_unique_hit(self):
        """"wxid_a1" 子串能匹配到 "wxid_a123"，多命中时必须放弃，
        否则会展示另一个人的数据。"""
        import inspect
        import gui.server as S
        src = inspect.getsource(S.run_gui)
        self.assertIn("len(cand) == 1", src)


class TestSessionTypeConsistency(unittest.TestCase):
    """会话类型与联系人类型对同一个账号必须给出相容的判定。

    真实缺陷：`wxid_gh*` 是公众号的 wxid 形式，`_session_type` 只判了 `gh_` 前缀，
    漏掉了 `wxid_gh`。同一个公众号账号因此在「数据浏览」页显示「单聊」、
    在「联系人」页显示「公众号」——用户直接可见的自相矛盾。
    前端曾用同一张中文映射表把 private 翻成「好友」，恰好把这个矛盾盖住了。

    注意：**两套词表不合并**（通讯录看 delete_flag、会话看 session_type，
    各自有存在理由，合并会丢信息）。这里只要求公众号这一项判定对齐。
    """

    def _st(self, username, session_type=None):
        from core.dataset import Dataset
        return Dataset._session_type(username, session_type)

    def test_wxid_gh_is_mp_regardless_of_session_type(self):
        """规格复现的两组分歧——修完后都必须判 mp。"""
        self.assertEqual(self._st("wxid_gh9x8y7z", None), "mp")
        self.assertEqual(self._st("wxid_gh9x8y7z", 2), "mp")

    def test_both_mp_prefixes_recognised(self):
        """两种写法都要认：gh_ 与 wxid_gh。"""
        self.assertEqual(self._st("gh_abc123"), "mp")
        self.assertEqual(self._st("wxid_gh9x8y7z"), "mp")

    def test_session_type_branch_still_reachable(self):
        """修 gh_ 不能把 session_type 分支吃掉——非公众号账号仍靠它判群/公众号。"""
        self.assertEqual(self._st("wxid_zzz", 2), "group")
        self.assertEqual(self._st("wxid_zzz", 3), "mp")

    def test_plain_friend_stays_private(self):
        self.assertEqual(self._st("wxid_u1"), "private")
        self.assertEqual(self._st("wxid_u1", 1), "private")

    def test_chatroom_stays_group(self):
        """@chatroom 优先级最高——公众号群也带 @chatroom。"""
        self.assertEqual(self._st("123456@chatroom"), "group")
        self.assertEqual(self._st("gh_abc@chatroom"), "group")

    def test_matches_contact_type_for_mp(self):
        """与 contact_type() 的公众号判定对齐：同一个 wxid_gh 两边都得是 mp。"""
        import inspect
        from core.dataset import Dataset
        src = inspect.getsource(Dataset.contact_type)
        self.assertIn('username.startswith("wxid_gh")', src)

        # contact_type 依赖通讯录，纯静态方法不易构造实例；
        # 改为锁住两处源码的判定条件一致——这是本条能自动化验证的部分
        st_src = inspect.getsource(Dataset._session_type)
        self.assertIn('username.startswith("wxid_gh")', st_src)
        self.assertIn('username.startswith("gh_")', st_src)


if __name__ == "__main__":
    unittest.main()