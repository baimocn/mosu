# -*- coding: utf-8 -*-
"""截断样本的四个 first* 里程碑必须一致标注（缺陷 2）。

背景：`contact_profile` 有 `CONTACT_MAX_MSGS` 上限，超限时样本只是**最近**
cap 条，会话真实的最早一条并不在样本里。

`firstMessage` 早已被打上 `sampledOnly`，但紧挨着的 `firstVoice` /
`firstImage` / `firstVideo` 没有。同一页面上：

    首条消息    2026-07-27 16:54  「样本内最早」   ← 谨慎
    第一条语音  2026-07-07 21:35                 ← 确定语气

用户完全有理由读成「他们第一次发语音是 7 月 7 日」。**只修一半比不修更糟**：
标注了一个反而让旁边没标的显得可信。

所以口径是：`truncated` 为真时，四个字段**一起**标；未截断时**都不标**
（保持字段干净，前端的 `visible` 也才能正常渲染）。
"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_analytics_guards import FakeDataset  # noqa: E402

# 真实 local_type（core/msgparse.py），必须用真值——kind 是由它决定的，
# 用FakeDataset 默认的「非文本即image」造不出 voice / video。
T_TEXT, T_IMAGE, T_VOICE, T_VIDEO = 1, 3, 34, 43
_KIND = {T_TEXT: "text", T_IMAGE: "image", T_VOICE: "voice", T_VIDEO: "video"}

FIRST_KEYS = ("firstMessage", "firstVoice", "firstImage", "firstVideo")
MEDIA_KEYS = ("firstVoice", "firstImage", "firstVideo")


def _distiller(n_msgs, media_at, session="wxid_a"):
    """造一个 ProfileDistiller：n_msgs 条消息，媒体放在指定下标。

    `media_at` 是 [(下标, local_type), ...]，顺序随意（不靠位置隐式配对）。
    """
    from core.profile import ProfileDistiller
    base = int(datetime(2026, 3, 1).timestamp())
    rows = []
    for i in range(n_msgs):
        lt = T_TEXT
        # 显式按 (下标, 类型) 配对，不靠排序去对齐顺序——
        # 之前靠 sorted(media_at) 去索引 (voice, image, video)，
        # 调用方一旦乱序传就静默错配（image 被当成 voice），测出来是假绿。
        for idx, media_type in media_at:
            if i == idx:
                lt = media_type
        rows.append((session, base + i * 86400, lt,
                     "wxid_me" if i % 2 else session, f"M{i}"))
    ds = FakeDataset([{"sessionId": session, "displayName": "张三",
                       "type": "private"}], rows)
    return ProfileDistiller(ds)


class TestTruncatedMilestonesSampled(unittest.TestCase):
    """截断时四个 first* 都要标 sampledOnly。"""

    def setUp(self):
        # 60 条、上限 30 → 截断；媒体放在 35/36/37，落在样本窗口内
        d = _distiller(60, [(35, T_VOICE), (36, T_IMAGE),
                            (37, T_VIDEO)]).contact_profile("wxid_a", max_msgs=30)
        self.assertIs(d["meta"]["truncated"], True, "前置条件：本例必须是截断样本")
        self.ms = d["milestones"]

    def test_fixture_really_has_media_milestones(self):
        """前置守卫：媒体必须真的落在样本里，否则下面的断言是空转。"""
        for k in MEDIA_KEYS:
            self.assertIsNotNone(self.ms.get(k),
                                 f"{k} 是 None——媒体没进样本窗口，本用例测不到东西")

    def test_first_message_still_flagged(self):
        """既有的 firstMessage 标注不能被这次改动弄丢。"""
        self.assertIs(self.ms["firstMessage"].get("sampledOnly"), True)

    def test_first_voice_flagged(self):
        self.assertIs(self.ms["firstVoice"].get("sampledOnly"), True,
                      "第一条语音没标 sampledOnly——用户会读成真实第一次发语音")

    def test_first_image_flagged(self):
        self.assertIs(self.ms["firstImage"].get("sampledOnly"), True,
                      "第一张图没标 sampledOnly")

    def test_first_video_flagged(self):
        self.assertIs(self.ms["firstVideo"].get("sampledOnly"), True,
                      "第一个视频没标 sampledOnly")

    def test_all_four_flagged_together(self):
        """四个字段口径必须一致——只标一部分比不标更误导。"""
        flags = {k: self.ms[k].get("sampledOnly") for k in FIRST_KEYS}
        self.assertEqual(set(flags.values()), {True},
                         f"四个 first* 的标注不一致：{flags}")

    def test_media_times_are_inside_sample_window(self):
        """确认「它们确实不是真实首条」——样本起点晚于会话真实起点。"""
        self.assertGreaterEqual(self.ms["firstVoice"]["time"],
                                self.ms["firstMessage"]["time"])


class TestUntruncatedMilestonesClean(unittest.TestCase):
    """未截断时四个字段都**不能**有 sampledOnly（保持字段干净）。"""

    def setUp(self):
        d = _distiller(12, [(3, T_VOICE), (4, T_IMAGE),
                            (5, T_VIDEO)]).contact_profile("wxid_a", max_msgs=100)
        self.assertIs(d["meta"]["truncated"], False,
                      "前置条件：本例必须未截断")
        self.ms = d["milestones"]

    def test_fixture_really_has_media_milestones(self):
        for k in MEDIA_KEYS:
            self.assertIsNotNone(self.ms.get(k), f"{k} 是 None，前置条件不成立")

    def test_first_message_not_flagged(self):
        self.assertNotIn("sampledOnly", self.ms["firstMessage"],
                         "未截断却标了 sampledOnly——正常数据被误标成样本")

    def test_first_voice_not_flagged(self):
        self.assertNotIn("sampledOnly", self.ms["firstVoice"])

    def test_first_image_not_flagged(self):
        self.assertNotIn("sampledOnly", self.ms["firstImage"])

    def test_first_video_not_flagged(self):
        self.assertNotIn("sampledOnly", self.ms["firstVideo"])

    def test_all_four_clean_together(self):
        """未截断时是「一个都不标」，不是「标一部分」。"""
        for k in FIRST_KEYS:
            self.assertNotIn("sampledOnly", self.ms[k],
                             f"未截断的{k} 带了 sampledOnly")

    def test_times_are_real_earliest(self):
        """未截断时它们就是真实最早——这正是「不标」成立的前提。"""
        self.assertEqual(self.ms["firstMessage"]["time"][:10], "2026-03-01")
        self.assertEqual(self.ms["firstVoice"]["time"][:10], "2026-03-04")


class TestMissingMediaNotTagged(unittest.TestCase):
    """没有某类媒体时那个字段是 None，不能凭空造出标注。"""

    def test_absent_media_stays_none(self):
        """全是文本的会话：三个 first* 都None，且不报错。"""
        from core.profile import ProfileDistiller
        base = int(datetime(2026, 3, 1).timestamp())
        rows = [("wxid_a", base + i * 86400, T_TEXT, "wxid_me", f"M{i}")
                for i in range(60)]
        ds = FakeDataset([{"sessionId": "wxid_a", "displayName": "张三",
                           "type": "private"}], rows)
        d = ProfileDistiller(ds).contact_profile("wxid_a", max_msgs=30)
        for k in MEDIA_KEYS:
            self.assertIsNone(d["milestones"].get(k),
                              f"{k} 不该凭空出现（会话里没有这类消息）")
        self.assertIs(d["milestones"]["firstMessage"].get("sampledOnly"), True)


class TestFrontendRendersTheFlag(unittest.TestCase):
    """前端要能渲染这些标注——徽标是泛用的，不按字段名硬编码。

    这条是**跨端契约**：后端加了标注，前端没改就会退化成
    「样本内最早: true」这种噪音行（等于白标）。
    """

    def setUp(self):
        self.src = (ROOT / "gui" / "assets" / "views-insight.js") \
            .read_text(encoding="utf-8")

    def test_detection_is_generic_not_field_specific(self):
        """`isSampledFirst` 必须只看 sampledOnly，不认 firstMessage 这种字段名。

        认字段名的话，后端给 firstVoice 加标注就不会被渲染——
        而这正是本缺陷的关键。
        """
        i = self.src.index("isSampledFirst(v) {")
        body = self.src[i:i + 400]
        self.assertIn("sampledOnly === true", body,
                      "isSampledFirst 不再看 sampledOnly，标注会失效")
        for field in FIRST_KEYS:
            self.assertNotIn(field, body,
                             f"isSampledFirst 里硬编码了 {field}——"
                             "换个字段名的标注就不渲染了")

    def test_sampled_only_key_is_hidden_from_users(self):
        """sampledOnly 是给渲染层的信号，不能作为字段露给用户。"""
        i = self.src.index("visible() {")
        body = self.src[i:i + 900]
        self.assertIn("sampledOnly", body,
                      "visible() 不再隐藏 sampledOnly——界面会出现"
                      "「样本内最早: true」这种噪音")

    def test_badge_wording_exists(self):
        """标注要有个专属措辞，不能与「第一条语音」共用。"""
        self.assertIn("样本内最早", self.src,
                      "缺少「样本内最早」措辞，标注与真实首条无法区分")

    def test_media_labels_use_definite_wording(self):
        """**记录现状供前端同事判断**：这三个字段目前是确定语气。

        如果前端沿用这些标签，截断样本上的「第一条语音」读起来
        仍像真实结论。本断言不要求改文案，只把现状钉住，
        免得以为后端标注已经解决了措辞问题。
        """
        for label in ("第一条语音", "第一张图片", "第一个视频"):
            self.assertIn(label, self.src,
                          f"标签「{label}」不见了——前端需同步确认截断时的措辞")


class TestMilestoneFlaggingIsNotSelfProfileWide(unittest.TestCase):
    """self_profile 没有 milestones，别把它卷进来。

    这条是防「顺手也给它加一层」的护栏：本人画像没有 first* 里程碑，
    加了就是死代码。
    """

    def test_self_profile_has_no_milestones(self):
        d = _distiller(30, [(3, T_VOICE), (4, T_IMAGE),
                            (5, T_VIDEO)]).self_profile()
        self.assertNotIn("milestones", d,
                         "self_profile 竟有 milestones——确认 contact_profile "
                         "的改动没有外溢")


if __name__ == "__main__":
    unittest.main()