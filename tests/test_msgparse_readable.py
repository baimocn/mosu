# -*- coding: utf-8 -*-
"""非文本类消息的「压成人话」行为。

这些消息在数据库里是一段 XML（通话记录、位置、红包…）。以前后端把原始
XML 原样当正文返回，界面里就是一片
`<voipmsg type="VoIPBubbleMsg">…<roomkey>0</roomkey>…`，
既看不懂、又把 roomid/token 这类内部字段暴露给用户。

本文件锁住：任何 XML 都不能再以原始形态出现在 content 里。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.msgparse import parse_message, _readable_from_xml, T_SYS, T_APPMSG  # noqa: E402


def _has_raw_xml(text: str) -> bool:
    """文本里是否还残留 XML 标签。"""
    s = (text or "").strip()
    return s.startswith("<")


class TestXmlNeverLeaks(unittest.TestCase):
    """核心断言：翻译结果绝不能是原始 XML。"""

    CASES = {
        "通话时长": '<voipmsg type="VoIPBubbleMsg"><VoIPBubbleMsg>'
                    '<msg><![CDATA[通话时长 00:37]]></msg>'
                    '<room_type>1</room_type><red_dot>false</red_dot>'
                    '<roomid>515431369734807303</roomid></voipmsg>',
        "通话-其它设备": '<voipmsg type="VoIPBubbleMsg"><VoIPBubbleMsg>'
                         '<msg><![CDATA[已在其它设备接听]]></msg></voipmsg>',
        "位置-有地点名": '<msg><location x="26.07" y="119.23" '
                         'label="某路口正北方向34米左右" poiname="福建农林大学" /></msg>',
        "位置-地点名为空": '<?xml version="1.0"?> <msg>\t<location x="26.07" y="119.27" '
                            'label="福州市鼓楼区杨桥西路50号" poiname="" /></msg>',
        "红包": '<img src="SystemMessages_HongbaoIcon.png"/>  你领取了某人的'
                '<_wc_custom_link_ color="#FD9931" href="weixin://x">红包</_wc_custom_link_>',
        "未知标签": '<unknownthing a="1"/>',
    }

    def test_no_case_returns_raw_xml(self):
        for name, xml in self.CASES.items():
            with self.subTest(case=name):
                out = _readable_from_xml(xml)
                self.assertTrue(out, f"{name}: 不能返回空串")
                self.assertFalse(_has_raw_xml(out), f"{name}: 仍返回原始 XML → {out[:60]}")

    def test_call_duration_extracted(self):
        out = _readable_from_xml(self.CASES["通话时长"])
        self.assertIn("通话时长 00:37", out)
        self.assertIn("通话", out)

    def test_location_prefers_poiname(self):
        out = _readable_from_xml(self.CASES["位置-有地点名"])
        self.assertIn("福建农林大学", out)
        self.assertNotIn("34米", out, "有地点名时不该退回详细地址")

    def test_location_falls_back_to_label(self):
        """poiname 为空时必须退到 label，而不是给出一个空的 [位置]。"""
        out = _readable_from_xml(self.CASES["位置-地点名为空"])
        self.assertIn("杨桥西路50号", out)

    def test_hongbao_text_extracted(self):
        out = _readable_from_xml(self.CASES["红包"])
        self.assertIn("领取了", out)
        self.assertIn("红包", out)

    def test_empty_xml_not_empty_string(self):
        """空输入也要给占位，返回空串会让界面出现一个空白气泡。"""
        self.assertTrue(_readable_from_xml(""))


class TestParseMessageBranches(unittest.TestCase):
    """真正走到 parse_message 的分支，防止只测到辅助函数。

    注意：真实库里 content 是「未压缩 XML 的 UTF-8 字节」或「zstd 压缩字节」，
    都是 bytes。传 str 会走 decompress 之外的路径、测不到真实分支——
    第一版测试就栽在这里（撤回消息误报失败）。
    """

    @staticmethod
    def _bytes(xml: str) -> bytes:
        return xml.encode("utf-8")

    def test_sys_branch_translates_hongbao(self):
        """红包在库里是 T_SYS，不是 T_APPMSG —— 这条分支最容易漏。"""
        xml = TestXmlNeverLeaks.CASES["红包"]
        m = parse_message(T_SYS, self._bytes(xml), None)
        self.assertFalse(_has_raw_xml(m["text"]), f"T_SYS 分支仍吐 XML: {m['text'][:60]}")
        self.assertIn("领取了", m["text"])

    def test_sys_branch_still_translates_revoke(self):
        """回归：撤回消息的翻译不能被本次改动带坏。"""
        xml = ('<sysmsg type="revokemsg"><revokemsg><replacemsg>'
               '<![CDATA["张三" 撤回了一条消息]]></replacemsg></revokemsg></sysmsg>')
        m = parse_message(T_SYS, self._bytes(xml), None)
        self.assertIn("撤回了一条消息", m["text"])

    def test_sys_branch_unknown_type_still_bounded(self):
        """未知的 sysmsg 类型也只能给中文占位，不能吐 XML。"""
        xml = '<sysmsg type="whatever"><foo>x</foo></sysmsg>'
        m = parse_message(T_SYS, self._bytes(xml), None)
        self.assertFalse(_has_raw_xml(m["text"]))

    def test_appmsg_empty_text_falls_back(self):
        """appmsg 解析不出 title 时不能留空串。"""
        xml = TestXmlNeverLeaks.CASES["红包"]
        m = parse_message(T_APPMSG, self._bytes(xml), None)
        self.assertTrue(m["text"], "appmsg 分支返回空串，界面会是空白气泡")
        self.assertFalse(_has_raw_xml(m["text"]))


if __name__ == "__main__":
    unittest.main()
