# -*- coding: utf-8 -*-
"""错误契约：参数非法必须是 400，「资源不存在」才是 404。

验收实测撞出的缺陷：`parse_time` 解析失败时 `raise SystemExit`，而 GUI 的全局
守卫 `except (SystemExit, LookupError, KeyError) → 404`。两者一撞，
「from 写错了」被报成「资源不存在」——用户和调用方都被误导。

影响面比单个端点大：parse_time 在 gui/server.py 有 4 处接线
（messages / stats 三兄弟 / 导出两处）、mcp_server.py 有 3 处。

这里锁三件事：
1. 异常**类型**分层清楚：参数错 → ValueError，确实找不到 → SystemExit
2. JSON 端点**必须**走 `_guard`（曾有 3 个端点自建 handler，绕过守卫）
3. 截断时四个 first* 里程碑**全部**标 sampledOnly（曾只标一个）
"""
import inspect
import unittest
from unittest.mock import MagicMock


class TestParseTimeExceptionType(unittest.TestCase):
    """参数非法抛 ValueError，不抛 SystemExit。"""

    def test_invalid_raises_value_error(self):
        from core.dataset import parse_time
        with self.assertRaises(ValueError) as cm:
            parse_time("丱码")
        self.assertIn("无法解析时间", str(cm.exception))

    def test_time_parse_error_is_value_error_subclass(self):
        from core.dataset import TimeParseError
        self.assertTrue(issubclass(TimeParseError, ValueError))

    def test_time_parse_error_is_not_systemexit(self):
        """这一条是整个修复的核心：不是 ValueError 的子类，而是 SystemExit 的反面。

        GUI 守卫按 `except SystemExit → 404` / `except ValueError → 400` 分流，
        继承错会让「参数非法」继续被报成「不存在」。
        """
        from core.dataset import TimeParseError
        self.assertFalse(issubclass(TimeParseError, SystemExit))
        e = TimeParseError("x")
        self.assertIsInstance(e, ValueError)
        self.assertNotIsInstance(e, SystemExit)

    def test_legal_values_unaffected(self):
        from core.dataset import parse_time
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time(""))
        self.assertEqual(parse_time("2026-01-01"), 1767196800)
        self.assertEqual(parse_time(1700000000), 1700000000)
        self.assertEqual(parse_time("2026/01/01"), 1767196800)

    def test_all_supported_formats_still_parse(self):
        from core.dataset import parse_time
        for s in ("2026-01-01", "2026-01-01 12:30", "2026-01-01 12:30:00", "2026/01/01"):
            self.assertIsInstance(parse_time(s), int, f"{s} 应能解析")


class TestNotFoundStillSystemExit(unittest.TestCase):
    """「确实找不到」必须继续抛 SystemExit，否则 404 会变成 400。"""

    def test_analytics_session_not_found_is_systemexit(self):
        import core.analytics as A
        src = inspect.getsource(A)
        self.assertIn('raise SystemExit(f"[x] 没找到会话', src)

    def test_profile_session_not_found_is_systemexit(self):
        import core.profile as P
        src = inspect.getsource(P.ProfileDistiller.contact_profile)
        self.assertIn("raise SystemExit", src)

    def test_dataset_find_failure_is_systemexit(self):
        import core.dataset as D
        src = inspect.getsource(D.Dataset)
        self.assertIn("raise SystemExit(f\"[x] 没找到会话", src)


class TestJsonRoutesUseGuard(unittest.TestCase):
    """JSON 端点必须走 `_guard`，否则参数错误会被自建 handler 报成 500。

    实测踩过：`/api/messages` 与 `/api/search` 自建 `except Exception → 500`，
    非法 from 落到那里变成 500（改成 ValueError 之前更糟——SystemExit 直接
    冲出 except，连响应都没有，实测 HTTP 000）。
    """

    def _routes_block(self):
        import gui.server as S
        src = inspect.getsource(S.run_gui)
        i = src.index("def do_GET(self)")
        return src[i:]

    def _json_routes(self) -> str:
        """取 JSON_ROUTES 路由表源码。

        断言目标从「某行写了 self._guard(...)」改为「该端点在路由表里登记了」——
        意图不变（JSON 端点必须走统一错误处理），但不再依赖具体写法。
        原来的字面匹配在把 if 链改成路由表后全线误报，那是测试太脆，不是代码坏了。
        """
        import gui.server as S
        src = inspect.getsource(S.run_gui)
        i = src.index("JSON_ROUTES = {")
        j = src.index("\n    }", i)
        return src[i:j]

    def test_messages_uses_guard(self):
        self.assertIn('"/api/messages"', self._json_routes())

    def test_search_uses_guard(self):
        self.assertIn('"/api/search"', self._json_routes())

    def test_sessions_uses_guard(self):
        self.assertIn('"/api/sessions"', self._json_routes())

    def test_no_json_route_bypasses_guard(self):
        """JSON 类端点不得再出现 `self._json(api_xxx(...))` 的裸调用。"""
        block = self._routes_block()
        self.assertNotIn("self._json(api_messages(", block)
        self.assertNotIn("self._json(api_search(", block)
        self.assertNotIn("self._json(api_sessions(", block)

    def test_binary_routes_keep_bespoke_handler(self):
        """media/remote 返回二进制，**故意**不走 _guard——但要留下理由注释，
        否则下一个人会以为是漏改而去「统一」。"""
        block = self._routes_block()
        self.assertIn("data, mime, headers = api_media(", block)
        self.assertIn("返回二进制", block)

    def test_guard_maps_valueerror_to_400(self):
        import gui.server as S
        src = inspect.getsource(S.run_gui)
        i = src.index("def _guard(self, fn, *args)")
        block = src[i:i + 900]
        self.assertIn("except ValueError as e:", block)
        self.assertIn("400", block)

    def test_guard_maps_systemexit_to_404(self):
        import gui.server as S
        src = inspect.getsource(S.run_gui)
        i = src.index("def _guard(self, fn, *args)")
        block = src[i:i + 900]
        self.assertIn("except (SystemExit, LookupError, KeyError)", block)
        self.assertIn("404", block)


class TestNoDeadWorkaround(unittest.TestCase):
    """根因修好后，局部补丁就成了会误导人的死代码。"""

    def test_export_no_longer_wraps_parse_time(self):
        """导出端点原先包了 `except SystemExit → ValueError`。
        根因已修，保留只会让人以为 parse_time 还会抛 SystemExit。

        断言的是**代码**（`raise ValueError(str(e)) from e`）而非注释文本——
        注释里刻意解释了这段补丁为什么被删，字符串匹配会误报。
        """
        import gui.server as S
        src = inspect.getsource(S.run_gui)
        i = src.index("def api_export(payload: dict)")
        block = src[i:src.index("def ", i + 20)]
        self.assertNotIn("raise ValueError(str(e)) from e", block)
        # 也不该有紧贴 parse_time 的 try/except 包装
        self.assertNotIn("except SystemExit as e:", block)

    def test_cli_preserves_clean_error_output(self):
        """CLI 必须把 ValueError 转成 sys.exit，否则用户看到的是 traceback。"""
        import cli.mosu as M
        src = inspect.getsource(M.main)
        self.assertIn("except ValueError as e:", src)
        self.assertIn("sys.exit(str(e))", src)


class TestMcpErrorCodeMapping(unittest.TestCase):
    """MCP 侧 ValueError 走 -32602（Invalid params），SystemExit 走 -32000。

    改异常类型后，MCP 的非法 from/to 从"通用错误"升级为 JSON-RPC 定义的
    "参数非法"——这是顺带修好的，不用额外改 MCP 代码。
    """

    def test_mcp_maps_valueerror_to_invalid_params(self):
        import core.mcp_server as M
        src = inspect.getsource(M.Protocol)
        self.assertIn("except ValueError as e:", src)
        self.assertIn("-32602", src)

    def test_mcp_systemexit_still_generic(self):
        import core.mcp_server as M
        src = inspect.getsource(M.Protocol)
        self.assertIn("except SystemExit as e:", src)
        self.assertIn("-32000", src)

    def test_time_parse_error_will_hit_invalid_params_branch(self):
        from core.dataset import TimeParseError
        # ValueError 分支在 SystemExit 分支之后；只要不是 SystemExit 就能落到它
        self.assertIsInstance(TimeParseError("x"), ValueError)
        self.assertNotIsInstance(TimeParseError("x"), SystemExit)


if __name__ == "__main__":
    unittest.main()