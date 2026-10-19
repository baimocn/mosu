# -*- coding: utf-8 -*-
"""README 与 MCP 工具集的防腐化测试。

背景：本轮交付前出现过一次「README 写 12 个工具，实际注册了 16 个」的误传。
查到最后发现两边都没错——是"写死的数字"必然会腐化：
加一个工具要记得改三处 README，改一处漏两处就开始骗人。

所以规则不是"把数字改对"，而是**不让数字存在**：
README 改为描述性表述 + 罗列工具名，由本测试双向锁住集合。

双向是关键：
- 只断言 `real <= doc` → README 写了不存在的工具名也发现不了
- 只断言 `doc <= real` → 漏写新工具没人提醒
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

# mosu_ 前缀的完整工具名（含下划线，不含括号等尾随符号）
_TOOL_RE = re.compile(r"mosu_[a-z_]+")

# 写死工具数目的各种说法。中英文都要盯——英文那段最容易漏改。
_HARDCODED = ("12 个工具", "(12 tools)", "12 工具", "12 个 MCP",
              "（12 工具）", "共 12 个")


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def _real_tools() -> set:
    from core.mcp_server import TOOLS
    return {t["name"] for t in TOOLS}


def _doc_tools() -> set:
    return set(_TOOL_RE.findall(_readme_text()))


class TestReadmeToolList(unittest.TestCase):
    def test_no_hardcoded_tool_count(self):
        """README 里不该再出现写死的工具数目。"""
        text = _readme_text()
        for bad in _HARDCODED:
            self.assertNotIn(bad, text, f"README 不该写死工具数「{bad}」")

    def test_doc_set_equals_real_set(self):
        """README 罗列的工具名 == 实际注册的，双向不多不漏。"""
        doc, real = _doc_tools(), _real_tools()
        missing = real - doc          # 漏写：新增工具忘了更新 README
        extra = doc - real            # 多写：README 里有个根本不存在的工具
        self.assertFalse(missing, f"README 漏写了工具：{sorted(missing)}")
        self.assertFalse(extra, f"README 写了不存在的工具：{sorted(extra)}")

    def test_readme_points_to_tools_list(self):
        """读者要能自己去核对清单——这是去掉数字后的补偿手段。"""
        self.assertIn("tools/list", _readme_text())

    def test_tool_list_section_keeps_all_names(self):
        """MCP Server 那一节必须仍然逐个列出工具名，不能改成纯描述。"""
        text = _readme_text()
        i = text.index("## MCP Server")
        section = text[i:text.index("## 隐私与合规", i)]
        names = set(_TOOL_RE.findall(section))
        self.assertEqual(names, _real_tools(),
                         "MCP Server 小节要完整列出全部工具名")

    def test_english_section_uses_prefix_wording(self):
        """英文特性列表也要去掉写死数字。"""
        text = _readme_text()
        i = text.index("### Features")
        en = text[i:text.index("### Privacy", i)]
        self.assertNotRegex(en, r"\(\d+\s+tools?\)")
        self.assertIn("mosu_", en)

    def test_no_hardcoded_test_count(self):
        """测试例数与工具数是同一类腐化：加一个用例就要记得改四处 README。
        同一份精神，一并去掉——具体例数跑一次 discover 就知道。"""
        text = _readme_text()
        self.assertNotRegex(text, r"\d+\s*例")
        self.assertNotRegex(text, r"\d+\s*(offline\s+)?(test\s+)?cases")


class TestToolRegistrySanity(unittest.TestCase):
    """顺带锁住工具集本身的形状——测试自己也要有地基。"""

    def test_names_are_prefixed_and_unique(self):
        names = [t["name"] for t in __import__("core.mcp_server", fromlist=["TOOLS"]).TOOLS]
        self.assertTrue(names)
        for n in names:
            self.assertTrue(n.startswith("mosu_"), f"{n} 缺少 mosu_ 前缀")
        self.assertEqual(len(names), len(set(names)), "工具名重复了")

    def test_every_tool_has_description_and_schema(self):
        from core.mcp_server import TOOLS
        for t in TOOLS:
            self.assertTrue(t.get("description"), f"{t['name']} 缺 description")
            self.assertIsInstance(t.get("inputSchema"), dict,
                                  f"{t['name']} 缺 inputSchema")


if __name__ == "__main__":
    unittest.main()
