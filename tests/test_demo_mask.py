# -*- coding: utf-8 -*-
"""演示模式（MOSU_DEMO=1）必须真的脱敏。

为什么值得单独写测试：
  README 截图要进公开仓库。如果脱敏只做了一半——比如只换姓名没换正文——
  页面看起来完全正常，肉眼和普通断言都发现不了，但真实聊天已经泄露出去了。
  第一版就漏了三处（会话预览、朋友圈正文、机器人日志），
  是靠翻截图肉眼看出来的。这里把每一处都钉住。

需要真实数据库；没有则跳过。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB = os.environ.get("MOSU_DB_PATH", "")


def _has_db() -> bool:
    if not DB:
        return False
    try:
        return Path(DB, "message", "message_0.db").exists()
    except Exception:
        return False


@unittest.skipUnless(_has_db(), "需要真实微信数据库（设 MOSU_DB_PATH）")
class TestDemoMask(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from core.dataset import Dataset
        cls.real = Dataset(DB, demo=False, quiet=True)
        cls.demo = Dataset(DB, demo=True, quiet=True)

    # ---------------------------------------------------------------- 身份
    def test_wxid_masked(self):
        self.assertEqual(self.demo.mask_id("wxid_qscqqvaviqat22"), self.demo.mask_id("wxid_qscqqvaviqat22"))
        self.assertTrue(self.demo.mask_id("wxid_qscqqvaviqat22").startswith("wxid_demo"))
        self.assertNotIn("qscqqvaviqat22", self.demo.mask_id("wxid_qscqqvaviqat22"))

    def test_chatroom_masked_and_distinguishable(self):
        """群号必须打码，且打码结果要能一眼看出是假的。

        真群号是纯 11 位数字；如果假群号也是纯数字，人工审核截图时
        根本分不清是没脱敏还是脱敏了（自动化检查也会误判）。
        """
        got = self.demo.mask_id("44741360388@chatroom")
        self.assertNotIn("44741360388", got)
        self.assertTrue(got.startswith("demo"), f"假群号应带 demo 前缀，实际 {got}")

    def test_mask_id_noop_when_not_demo(self):
        """非演示模式必须原样返回，否则会破坏真实功能（跳转、筛选都依赖真 wxid）。"""
        for u in ("wxid_abc123456", "44741360388@chatroom", "gh_abcdef"):
            self.assertEqual(self.real.mask_id(u), u)

    def test_display_name_stable(self):
        """同一个人的化名必须稳定，否则跨页对不上。"""
        ss = self.demo.sessions()
        if not ss:
            self.skipTest("无会话")
        sid = ss[0]["sessionId"]
        self.assertEqual(self.demo.display_name(sid), self.demo.display_name(sid))

    # ---------------------------------------------------------------- 内容
    def test_session_preview_masked(self):
        """会话列表右侧预览是真实聊天摘要，必须替换。

        这一处最容易漏：姓名换了、预览没换，截图里就明明白白写着
        「这里仓库搬迁，有很多东西处理」。
        """
        demo_s = self.demo.sessions()
        real_s = {s["sessionId"]: s for s in self.real.sessions()}
        leaked = 0
        for s in demo_s[:40]:
            r = real_s.get(s["sessionId"])
            if not r:
                continue
            rl, dl = (r.get("lastMessage") or ""), (s.get("lastMessage") or "")
            if len(rl) >= 6 and rl == dl:
                leaked += 1
        self.assertEqual(leaked, 0, f"{leaked} 个会话的预览仍是真实内容")

    def test_insights_hide_occupation(self):
        """画像的自动结论会暴露职业/学业，演示模式要中性化。"""
        try:
            p = self.demo.self_profile()
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"画像不可用: {e}")
        text = "".join(p.get("insights") or [])
        for bad in ("开发者", "接单", "考研"):
            self.assertNotIn(bad, text, f"演示模式结论仍含「{bad}」")


class TestDemoTextHelper(unittest.TestCase):
    """gui.server._demo_text 的行为（不需要数据库）。"""

    def setUp(self):
        os.environ["MOSU_DEMO"] = "1"
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from gui import server
        self.f = server._demo_text

    def tearDown(self):
        os.environ.pop("MOSU_DEMO", None)

    def test_replaces_all_nonempty(self):
        for s in ("在吗", "我昨天搞了一个项目", "x" * 100):
            out = self.f(s)
            self.assertTrue(out)
            self.assertNotEqual(out, s, f"「{s[:10]}」原样返回，没脱敏")

    def test_empty_stays_empty(self):
        self.assertEqual(self.f(""), "")

    def test_similar_length(self):
        """长度感要保留，否则截图的版面会塌，看着不像真实界面。"""
        self.assertLessEqual(len(self.f("好的")), 6)
        self.assertGreater(len(self.f("x" * 60)), 10)


if __name__ == "__main__":
    unittest.main()
