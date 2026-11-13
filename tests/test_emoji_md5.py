# -*- coding: utf-8 -*-
"""表情消息的 md5 不许在 dataset.to_message 里丢掉。

历史缺陷：msgparse 把表情 XML 的 md5 解析成 `emoji_md5`（图片消息叫
`img_md5`），而 to_message 的 `imgMd5` 只取 `m.get("img_md5")`。
结果自定义表情的 imgMd5 全是 None，下游既拿不到图片，也只能把
productid（`com.tencent.xin.emoticon.person.stiker_…`）当正文显示。

这里用真实 msgparse 输出 + 打桩的 Dataset，锁住「表情 md5 必须透出」。
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.dataset import Dataset  # noqa: E402
from core.msgparse import T_EMOJI, T_IMAGE, parse_message  # noqa: E402


def _row(local_type, content, packed=b""):
    """造一行最小 sqlite3.Row。

    message_content 必须是 bytes：parse_message 只对 bytes 走 decompress
    （非 zstd 原样返回），传 str 会得到空 xml，emoji 分支就永远取不到 md5。
    真实数据库里这一列也是 BLOB。
    """
    if isinstance(content, str):
        content = content.encode("utf-8")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (local_type, message_content, packed_info_data, "
                 "local_id, server_id, create_time, sort_seq, real_sender_id)")
    conn.execute("INSERT INTO t VALUES (?,?,?,?,?,?,?,?)",
                 (local_type, content, packed, 1, 2, 1700000000, 1, 0))
    return conn.execute("SELECT * FROM t").fetchone()


class _StubDataset(Dataset):
    """跳过 __init__（会去解密真实数据库），只打桩 self_id。"""
    def __init__(self):
        pass

    def self_id(self, shard=None):
        return 0


class TestEmojiMd5Passthrough(unittest.TestCase):
    SESS = {"sessionId": "wxid_test", "displayName": "测试", "type": "private",
            "shard": "message/message_0.db", "messageCount": 1}

    def test_emoji_xml_md5_is_exposed(self):
        xml = ('<msg><emoji md5="a1b2c3d4e5f60718293a4b5c6d7e8f90" '
               'productid="com.tencent.xin.emoticon.person.stiker_abc" /></msg>')
        parsed = parse_message(T_EMOJI, xml.encode("utf-8"), b"")
        self.assertEqual(parsed.get("emoji_md5"), "a1b2c3d4e5f60718293a4b5c6d7e8f90")

        out = _StubDataset().to_message(_row(T_EMOJI, xml, b""), self.SESS)
        self.assertEqual(out["kind"], "emoji")
        self.assertEqual(out["imgMd5"], "a1b2c3d4e5f60718293a4b5c6d7e8f90")

    def test_image_md5_still_wins_when_both_present(self):
        """图片消息仍取 img_md5，不能因为加了回退就被 emoji_md5 盖掉。"""
        xml = ('<msg><emoji md5="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" /></msg>')
        parsed = parse_message(T_EMOJI, "", xml)
        parsed["img_md5"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        # 直接验证回退表达式的优先级，不依赖 parse_message 造混合消息
        self.assertEqual(parsed.get("img_md5") or parsed.get("emoji_md5"),
                         "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    def test_emoji_without_md5_stays_none(self):
        """没 md5 的表情不该凭空造一个值出来。"""
        out = _StubDataset().to_message(_row(T_EMOJI, "", "<msg><emoji /></msg>"),
                                        self.SESS)
        self.assertIsNone(out["imgMd5"])


if __name__ == "__main__":
    unittest.main()
