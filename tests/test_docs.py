# -*- coding: utf-8 -*-
"""README 与 MCP 工具集的防腐化测试。

背景：本轮出现过一次「README 写 12 个工具，实际注册了 16 个」的误传。
复核后发现**两边都没错**——是复核方数错了。README 里的「12」是对的。

所以这里刻意**不改 README 一个字**：
写死数字让读者能自己核对（"一组工具"读者反而无从验证），
而「写死数字会腐化」这个风险是真的，靠测试锁住比改措辞更有效。

三条断言：
1. README 里所有"N 工具 / N tools"的数字 == len(TOOLS)
2. 反向锁基线：len(TOOLS) == 12（将来加工具必须同步改 README，测试会红）
3. 双向名称集合：README 的 mosu_* 集合 == TOOLS 名字集合
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

# mosu_ 前缀的完整工具名
_TOOL_RE = re.compile(r"mosu_[a-z_]+")

# README 里写死的工具数目。中文「12 个工具」与英文「(12 tools)」两种写法
# 都可能漏改，所以两种都抓。顺序有讲究：先抓长的，避免「12 个工具」被
# 「12 工具」的短模式先吃掉一部分。
_COUNT_RES = [
    re.compile(r"(\d+)\s*个?\s*工具"),      # 12 个工具 / 12 工具
    re.compile(r"(\d+)\s*tools", re.I),      # (12 tools) / 12 tools
]

# 与工具数无关的数字（测试例数之类）——这些不在本测试管辖范围内，
# 但要确认没被上面的正则误抓。
_COUNT_TOTAL = 12


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def _real_tools() -> set:
    from core.mcp_server import TOOLS
    return {t["name"] for t in TOOLS}


def _doc_tool_names() -> set:
    return set(_TOOL_RE.findall(_readme_text()))


def _declared_counts() -> list:
    """README 里所有写死的工具数目。中英文都抓。"""
    text = _readme_text()
    found = []
    for rx in _COUNT_RES:
        for m in rx.finditer(text):
            # 记录所在行，便于失败时定位
            line = text[:m.start()].count("\n") + 1
            found.append((int(m.group(1)), line, m.group(0).strip()))
    return found


class TestReadmeMcpToolCountMatchesRegistry(unittest.TestCase):
    """规格指定的验收名。README 数字必须与实际注册数一致。"""

    def test_readme_counts_all_equal_registry(self):
        real = len(__import__("core.mcp_server", fromlist=["TOOLS"]).TOOLS)
        found = _declared_counts()
        self.assertTrue(found, "README 里一个工具数目都没抓到——正则失效了？")
        for n, line, raw in found:
            self.assertEqual(n, real,
                             f"README 第 {line} 行写「{raw}」，"
                             f"但 TOOLS 实际注册 {real} 个")

    def test_registry_baseline_is_twelve(self):
        """反向锁基线。将来加工具必须同步改 README，否则这条会红——
        这正是「防腐化靠测试」的意义：提醒而非改写。"""
        from core.mcp_server import TOOLS
        self.assertEqual(len(TOOLS), _COUNT_TOTAL)

    def test_both_languages_are_covered(self):
        """中文与英文两处都得有数目声明，否则上面的循环只覆盖了一半。"""
        text = _readme_text()
        self.assertRegex(text, r"\d+\s*个?\s*工具")
        self.assertRegex(text, r"\d+\s*tools", )

    def test_tool_name_set_is_bidirectional(self):
        """既不许多写也不许漏写。"""
        doc, real = _doc_tool_names(), _real_tools()
        missing = real - doc          # 漏写：新增工具忘了更新 README
        extra = doc - real            # 多写：README 里有个根本不存在的工具
        self.assertFalse(missing, f"README 漏写了工具：{sorted(missing)}")
        self.assertFalse(extra, f"README 写了不存在的工具：{sorted(extra)}")

    def test_mcp_section_lists_every_tool(self):
        """MCP Server 那一节要逐个列出，不能只写数目。"""
        text = _readme_text()
        i = text.index("## MCP Server")
        section = text[i:text.index("## 隐私与合规", i)]
        names = set(_TOOL_RE.findall(section))
        self.assertEqual(names, _real_tools())

    def test_registry_names_are_prefixed_and_unique(self):
        """测试自身也要有地基：抓取靠 mosu_ 前缀，前缀乱了整套就失效。"""
        from core.mcp_server import TOOLS
        names = [t["name"] for t in TOOLS]
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
