# -*- coding: utf-8 -*-
"""AI 对话语料提取的清洗规则测试。

重点不是"能不能跑"，而是**哪些东西必须被丢掉**。
这个模块的价值全在"剔干净"上——抽错一条系统提示词，
就相当于把 AI 的程序员口吻当成用户自己说话，画像直接失真。

所有用例都来自真实数据里实测命中的样本，不是编的。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.aicorpus import (       # noqa: E402
    MIN_LEN, extract_user_text, project_from_dir, strip_injections,
)


class TestUserQueryExtraction(unittest.TestCase):
    """`<user_query>` 是最可信的用户原话来源。"""

    def test_extracts_from_user_query(self):
        raw = "<user_query>帮我修一下这个 bug</user_query>"
        text, kind = extract_user_text(raw)
        self.assertEqual(kind, "user")
        self.assertEqual(text, "帮我修一下这个 bug")

    def test_user_query_inside_system_reminder(self):
        """实测 2182 条：system-reminder 开头，但内部包着真实输入。

        如果把「整条是注入」的判断放在前面，会把这些全丢掉——
        我第一版就这么写，总量从 2002 掉到 177。
        """
        raw = (
            '<system-reminder data-role="user-context">\n<user_info>\n'
            "OS Version: win32\n</user_info>\n</system-reminder>\n"
            "<user_query>把库迁进 SQL Server 标准数据目录</user_query>"
        )
        text, kind = extract_user_text(raw)
        self.assertEqual(kind, "user")
        self.assertIn("SQL Server", text)

    def test_injection_inside_user_query_is_stripped(self):
        """用户说"继续"时，同一轮里会挂后台任务通知，必须剥掉。"""
        raw = (
            "<user_query>继续\n\n下一步\n\n"
            "<task-notification>\n<task-id>yy38Sw</task-id>\n"
            "<status>completed</status>\n</task-notification></user_query>"
        )
        text, _ = extract_user_text(raw)
        self.assertIn("继续", text)
        self.assertNotIn("task-notification", text)
        self.assertNotIn("yy38Sw", text)

    def test_multiple_user_query_joined(self):
        raw = "<user_query>第一句</user_query>中间废话<user_query>第二句</user_query>"
        text, _ = extract_user_text(raw)
        self.assertIn("第一句", text)
        self.assertIn("第二句", text)


class TestPureInjectionDropped(unittest.TestCase):
    """整条都是系统注入的，必须一条不留。"""

    def test_task_notification_dropped(self):
        raw = (
            "<task-notification>\n<task-id>abc</task-id>\n"
            "<status>completed</status>\n</task-notification>"
        )
        self.assertEqual(extract_user_text(raw), ("", ""))

    def test_task_notification_with_trailing_instruction_dropped(self):
        """关键用例：标签剥掉后会残留系统写给 AI 的指令。

        实测污染了几百条——`Use the TaskOutput tool with task_id=…`
        这段被当成用户输入收进语料。
        """
        raw = (
            "<task-notification>\n<task-id>QsMDMJ</task-id>\n</task-notification>\n"
            'Use the TaskOutput tool with task_id="QsMDMJ" to retrieve unread output '
            "if you need to act on it. For background shell tasks, pass full=true."
        )
        text, _ = extract_user_text(raw)
        self.assertEqual(text, "")

    def test_error_recovery_reminder_dropped(self):
        raw = ('<system-reminder data-role="error-recovery">'
               "Network error occurred, please continue</system-reminder>")
        self.assertEqual(extract_user_text(raw), ("", ""))

    def test_craft_mode_dropped(self):
        raw = ("<craft_mode>You are now in Agent mode.</craft_mode>")
        self.assertEqual(extract_user_text(raw), ("", ""))

    def test_continue_instruction_dropped(self):
        """上下文压缩后的续接指令——系统叫 AI 继续，不是人说话。"""
        raw = ("Please continue with the conversation based on the summarized "
               "context above. Maintain the same level of detail as before.")
        self.assertEqual(extract_user_text(raw), ("", ""))

    def test_automation_prompt_dropped(self):
        """定时任务注入的执行模板（实测 16 条）。

        它把真实指令放在 `<user_query>` 里，但内容全是调度器写的
        "You MUST follow these steps in order:"，不能算用户语料。
        """
        raw = ("<user_query>.\n\nYou MUST follow these steps in order:\n"
               "1. Read D:\\Desktop\\x\\.workbuddy\\memory\\automations\\a\\memory.md\n"
               "2. Execute the task as specified in the user query.</user_query>")
        self.assertEqual(extract_user_text(raw), ("", ""))


class TestSummaryChannel(unittest.TestCase):
    """压缩摘要要保留但单独标记——它不是原话，信息密度却最高。"""

    def test_summary_kept_as_summary_kind(self):
        raw = ("<conversation_history_summary>## 1. Primary Request\n"
               "用户要求修复白屏问题，已完成 A/B 两项。</conversation_history_summary>")
        text, kind = extract_user_text(raw)
        self.assertEqual(kind, "summary")
        self.assertIn("白屏", text)

    def test_cb_summary_dropped(self):
        """`<cb_summary>` 是另一层压缩外壳，内容不是本轮真话。"""
        raw = ("<cb_summary>\nSummary of the conversation so far:\n"
               "<conversation_history_summary>旧内容</conversation_history_summary>\n"
               "</cb_summary>")
        text, kind = extract_user_text(raw)
        # 剥掉 cb_summary 外壳后，内层 conversation_history_summary 也不该当正文
        self.assertNotIn("旧内容", text)


class TestQuoteAndAttachment(unittest.TestCase):
    def test_message_quote_keeps_inner_text(self):
        """引用块里是用户自己之前说过的话，剥外壳留正文。"""
        raw = ('<user_query><message_quote type="text" source_message_id="x">\n'
               "<original_text><![CDATA[只在正式交付成果时贴]]></original_text>\n"
               "</message_quote></user_query>")
        text, _ = extract_user_text(raw)
        self.assertIn("只在正式交付成果时贴", text)
        self.assertNotIn("message_quote", text)
        self.assertNotIn("CDATA", text)

    def test_attachment_placeholder_stripped(self):
        raw = "<user_query>现在能识别吗 @image#1:Clipboard_Screenshot.png</user_query>"
        text, _ = extract_user_text(raw)
        self.assertNotIn("@image#1", text)
        self.assertIn("现在能识别吗", text)


class TestUnknownInjectionFailClosed(unittest.TestCase):
    """没见过的注入格式 → 宁可丢，也不能当成用户说的话。"""

    def test_unknown_tag_prefix_dropped(self):
        raw = "<some_new_injection_type>我们改主意了</some_new_injection_type>"
        text, _ = extract_user_text(raw)
        self.assertEqual(text, "")

    def test_plain_text_kept(self):
        """普通用户输入不能被误杀——这是最基本的回归。"""
        raw = "算了，先按这个方向做吧"
        text, kind = extract_user_text(raw)
        self.assertEqual(kind, "user")
        self.assertEqual(text, raw)

    def test_short_trusted_input_kept(self):
        """`<user_query>` 里的短输入是用户亲手敲的，必须保留。

        实测 301 条被 MIN_LEN=8 误杀，包括「请继续执行任务」×50、
        「在吗」「探一下」「A」「B」。短指令本身就是重要信号
        （说明他倾向下短语而非长需求），过滤掉会把"真实说话长度"整体拉长。
        """
        for raw in ("<user_query>继续</user_query>",
                    "<user_query>A</user_query>",
                    "<user_query>在吗</user_query>"):
            text, kind = extract_user_text(raw)
            self.assertEqual(kind, "user", f"{raw} 被误杀")
            self.assertTrue(text)

    def test_dot_only_dropped(self):
        """定时任务里 `<user_query>.</user_query>` 只有句点，不是真话。"""
        self.assertEqual(extract_user_text("<user_query>.</user_query>"), ("", ""))


class TestProjectName(unittest.TestCase):
    def test_strips_drive_letter(self):
        """盘符段要丢掉，否则项目名一律以 `d-` 开头，筛选时看不出区别。"""
        self.assertEqual(project_from_dir("d-Desktop-微信解密软件"), "Desktop-微信解密软件")

    def test_keeps_c_users_suffix(self):
        """`c-Users-35955-WorkBuddy-2026-09-07-20-59-21` 要保留可辨识的尾段。"""
        name = project_from_dir("c-Users-35955-WorkBuddy-2026-09-07-20-59-21")
        self.assertIn("WorkBuddy", name)

    def test_does_not_invent_paths(self):
        """不能被 `--` 骗着造出假路径：目录名里的连字符可能本来就是名字的一部分。"""
        name = project_from_dir("d-Desktop-墨衍-项目")
        self.assertNotIn("/", name)
        self.assertIn("墨衍", name)


class TestStripInjections(unittest.TestCase):
    def test_returns_empty_for_pure_injection(self):
        raw = "<task-notification>x</task-notification>"
        rest, _ = strip_injections(raw)
        self.assertEqual(rest, "")

    def test_keeps_plain_text(self):
        rest, _ = strip_injections("正常的一句话")
        self.assertEqual(rest, "正常的一句话")

    def test_reports_summary_presence(self):
        _, has = strip_injections(
            "<conversation_history_summary>x</conversation_history_summary>")
        self.assertTrue(has)


if __name__ == "__main__":
    unittest.main()
