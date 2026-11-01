# -*- coding: utf-8 -*-
"""MCP 工具契约的防腐化测试（不依赖真实微信数据）。

背景（2026-11-02）：白墨批「用不了，你没有进行端到端所有模块测试是看不到报错的」。
补测后挖出四个真问题，全都是「单元测试能拦住、而我当时没写」的那类：

1. `mosu_contact_info` 用 `a["name"]` 硬下标取参 → 模型漏参数时收到
   `KeyError: 'name'`，它既不知道这是缺参数也不知道该补什么。
2. `mosu_stats` 的 schema 把 `kind` 标成必填，实现却有默认值 `global` ——
   模型不传就直接被 schema 挡住，可它明明可以不传。
3. `mosu_export` 导出十万级会话要 491 秒，而 MCP 客户端默认 60 秒超时 →
   必然失败。限量必须下推到 SQL，不能读完再切。
4. 部分工具 schema 声明的参数实现根本没读（传了白传）。

这些断言都不碰数据库，纯静态 + 纯函数，能在 CI 里跑。
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "core" / "mcp_server.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _tools():
    from core.mcp_server import TOOLS
    return TOOLS


class TestSchemaWellFormed(unittest.TestCase):
    """schema 自身的合法性。"""

    def test_every_tool_has_schema_and_description(self):
        for t in _tools():
            self.assertTrue(t.get("description"), f"{t['name']} 缺 description")
            self.assertEqual((t.get("inputSchema") or {}).get("type"), "object",
                             f"{t['name']} 的 inputSchema 不是 object")

    def test_required_keys_exist_in_properties(self):
        """required 里写了 properties 里没有的字段 → 模型永远填不出来。"""
        for t in _tools():
            sch = t.get("inputSchema") or {}
            props = set((sch.get("properties") or {}).keys())
            for k in sch.get("required") or []:
                self.assertIn(k, props, f"{t['name']} 的 required 里有未声明的 {k}")

    def test_enum_values_are_non_empty(self):
        for t in _tools():
            props = (t.get("inputSchema") or {}).get("properties") or {}
            for k, p in props.items():
                if "enum" in p:
                    self.assertTrue(p["enum"], f"{t['name']}.{k} 的 enum 是空的")

    def test_kind_defaults_are_not_required(self):
        """有默认值的参数不该标 required。

        mosu_stats 的 kind 曾有这问题：schema 标必填、实现给了默认 global，
        模型不传就被拦，可它本来可以不传。
        """
        stats = next(t for t in _tools() if t["name"] == "mosu_stats")
        self.assertNotIn("kind", stats["inputSchema"].get("required") or [])


class TestParamValidation(unittest.TestCase):
    """入参校验：参数错时给的是人话，不是 Python 异常。"""

    def setUp(self):
        from core.mcp_server import McpServer
        self.validate = McpServer._validate

    def _assert_rejects(self, tool, args, want_in_msg):
        with self.assertRaises(ValueError) as cm:
            self.validate(tool, args)
        msg = str(cm.exception)
        self.assertIn(want_in_msg, msg, f"错误信息不通俗：{msg}")
        for bad in ("KeyError", "TypeError", "Traceback"):
            self.assertNotIn(bad, msg, f"把 Python 异常名漏给模型了：{msg}")

    def test_missing_required_is_readable(self):
        self._assert_rejects("mosu_contact_info", {}, "缺少必填参数")
        self._assert_rejects("mosu_messages", {}, "缺少必填参数")
        self._assert_rejects("mosu_search", {}, "缺少必填参数")

    def test_missing_message_includes_description(self):
        """缺参数时要带上这个参数是干嘛的，模型才知道填什么。"""
        with self.assertRaises(ValueError) as cm:
            self.validate("mosu_contact_info", {})
        self.assertIn("name", str(cm.exception))
        self.assertIn("备注", str(cm.exception))   # 来自 schema description

    def test_bad_enum_is_readable(self):
        self._assert_rejects("mosu_stats", {"kind": "瞎写"}, "只能是")

    def test_bad_integer_is_readable(self):
        self._assert_rejects("mosu_messages", {"session": "x", "limit": "很多"}, "要整数")

    def test_valid_args_pass(self):
        # 不能误杀正常入参
        for tool, args in [
            ("mosu_status", {}),
            ("mosu_messages", {"session": "某会话", "limit": 5}),
            ("mosu_stats", {"kind": "global"}),
            ("mosu_stats", {}),                        # kind 可省
            ("mosu_contact_info", {"name": "某人"}),
            ("mosu_export", {"session": "x", "format": "txt", "max_messages": 100}),
        ]:
            self.validate(tool, args)   # 不抛就算过


class TestNoHardIndexArgs(unittest.TestCase):
    """实现里不许用 a["x"] 硬下标取参。

    硬下标在缺参数时抛 KeyError，绕过校验层直接进模型上下文。
    全部改成 a.get() 后，即使校验被绕过也只是拿到 None，不会崩栈。
    """

    def _funcs(self):
        tree = ast.parse(SERVER.read_text(encoding="utf-8"))
        return [n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name.startswith("t_mosu_")]

    def test_no_hard_subscript_on_args(self):
        bad = []
        for fn in self._funcs():
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name) \
                        and sub.value.id in ("a", "args") \
                        and isinstance(sub.slice, ast.Constant) \
                        and isinstance(sub.slice.value, str):
                    bad.append(f"{fn.name[2:]}: a[{sub.slice.value!r}]")
        self.assertFalse(bad, f"这些地方还在硬下标取参：{bad}")


class TestSchemaImplConsistency(unittest.TestCase):
    """schema 声明的参数，实现必须真的读；实现读的，schema 必须声明。

    否则要么「模型传了没用」，要么「模型不知道该传」。
    """

    def _declared_and_used(self):
        from core.mcp_server import TOOLS
        declared = {t["name"]: set((t.get("inputSchema") or {}).get("properties") or {})
                    for t in TOOLS}

        tree = ast.parse(SERVER.read_text(encoding="utf-8"))
        # helper（_lim 等）读的参数要合并进调用它的工具
        helper_keys = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("_"):
                continue
            keys = {s.args[0].value for s in ast.walk(node)
                    if isinstance(s, ast.Call) and isinstance(s.func, ast.Attribute)
                    and s.func.attr == "get" and s.args
                    and isinstance(s.func.value, ast.Name)
                    and s.func.value.id in ("a", "args")
                    and isinstance(s.args[0], ast.Constant)
                    and isinstance(s.args[0].value, str)}
            if keys:
                helper_keys[node.name] = keys

        used = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("t_mosu_"):
                continue
            keys = set()
            for s in ast.walk(node):
                if isinstance(s, ast.Call) and isinstance(s.func, ast.Attribute) \
                        and s.func.attr == "get" and s.args \
                        and isinstance(s.func.value, ast.Name) \
                        and s.func.value.id in ("a", "args") \
                        and isinstance(s.args[0], ast.Constant) \
                        and isinstance(s.args[0].value, str):
                    keys.add(s.args[0].value)
                if isinstance(s, ast.Call) and isinstance(s.func, ast.Attribute) \
                        and s.func.attr in helper_keys:
                    keys |= helper_keys[s.func.attr]
            used[node.name[2:]] = keys
        return declared, used

    def test_declared_params_are_actually_read(self):
        declared, used = self._declared_and_used()
        problems = []
        for tool, decl in declared.items():
            impl = used.get(tool, set())
            missing = (decl - {"account"}) - impl
            if missing:
                problems.append(f"{tool} 声明了但没读：{sorted(missing)}")
        self.assertFalse(problems, "；".join(problems))

    def test_used_params_are_declared(self):
        declared, used = self._declared_and_used()
        problems = []
        for tool, u in used.items():
            extra = u - declared.get(tool, set()) - {"account"}
            if extra:
                problems.append(f"{tool} 读了但没声明：{sorted(extra)}")
        self.assertFalse(problems, "；".join(problems))


if __name__ == "__main__":
    unittest.main()


class TestExportMaxMessages(unittest.TestCase):
    """导出限量必须下推到 SQL，不能「读完再切」。

    实测：15.8 万条的大群，全量 html 要 491 秒；限量逻辑放在内存里切
    仍然要 321 秒（因为还是把 15.8 万条读进来排序）。下推到 SQL 后 1 秒。
    MCP 客户端默认 60 秒超时，这个差别就是「能用」和「用不了」。
    """

    def test_max_messages_param_is_declared(self):
        exp = next(t for t in _tools() if t["name"] == "mosu_export")
        props = (exp["inputSchema"] or {}).get("properties") or {}
        self.assertIn("max_messages", props)
        self.assertEqual(props["max_messages"]["type"], "integer")

    def test_limit_pushed_down_to_sql(self):
        """源码里必须出现 desc=True 的限量读取，而不是读完再切片。"""
        src = SERVER.read_text(encoding="utf-8")
        self.assertIn("iter_session_rows(sess, desc=want > 0, limit=want)", src,
                      "html 导出的限量没下推到 SQL —— 大群会超时")
        self.assertIn('order="desc", from_ts=frm', src,
                      "文本导出的限量没下推到 SQL —— 大群会超时")

    def test_max_messages_rejects_non_integer(self):
        from core.mcp_server import McpServer
        with self.assertRaises(ValueError):
            McpServer._validate("mosu_export", {"max_messages": "很多"})


class TestPiTimeoutBudget(unittest.TestCase):
    """Pi 的 MCP 超时配置要覆盖墨溯最慢的工具。

    墨溯实测耗时（23 万消息库）：
        profile 全库 ~19s / report ~6s / export 大群全量 ~491s
    Pi 的 MCP 默认 timeout 是 60 秒 → export 必然超时。
    """

    def test_timeout_covers_slowest_tool(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import install_pi_mcp as ipm
        t = ipm.mcp_entry().get("timeout")
        self.assertIsNotNone(t, "mcp_entry 没设 timeout，Pi 会用默认 60 秒")
        self.assertGreaterEqual(t, 300,
                                f"timeout={t}s 覆盖不了 export 全量导出（实测 491s）")
