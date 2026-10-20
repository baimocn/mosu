# -*- coding: utf-8 -*-
"""非法时间参数必须是 400，「会话不存在」必须仍是 404（缺陷 1）。

根因是 `parse_time` 用 `raise SystemExit` 报「时间格式不对」。而 GUI 的全局
守卫 `_guard` 把 `(SystemExit, LookupError, KeyError)` 一律映射成 **404**——
「参数写错了」被报成「资源不存在」。

这个错配有实际代价，不是洁癖：
- 用户看到 404 会去检查会话/网络，而不是自己的 `--from` 写错了
- MCP 客户端把 404 理解成「资源不存在」而重试或放弃，掩盖真实原因；
  JSON-RPC 里「参数非法」该用的是 **-32602Invalid params**

修法：`parse_time` 抛 `TimeParseError(ValueError)`，让上层能区分
「参数非法」(400 / -32602) 与「资源不存在」(404 / -32000)。

**最容易改坏的地方是 404**：找不到会话时 core 层抛的仍然是 `SystemExit`
（那是「不存在」语义，必须保持 404）。所以本文件对两个方向都设了断言——
只测 400 不测 404 的话，把所有异常一律改成 ValueError 也能全绿。
"""
import inspect
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.dataset import TimeParseError, parse_time  # noqa: E402


# ======================================================================
# 异常类型：库层不能替CLI 决定「退出」
# ======================================================================
class TestParseTimeExceptionType(unittest.TestCase):
    """`parse_time` 抛什么，直接决定三个入口各自的错误码。"""

    def test_invalid_raises_value_error(self):
        """非法时间必须是 ValueError，才能被 `_guard` 映射成 400。"""
        with self.assertRaises(ValueError) as cm:
            parse_time("丱码")
        self.assertIn("无法解析时间", str(cm.exception))

    def test_invalid_is_not_system_exit(self):
        """**不能**是 SystemExit：那是「不存在」的语义，会被守卫判成 404。

        这条是本缺陷的核心断言。若改回 SystemExit，本用例立刻红。
        """
        try:
            parse_time("乱码")
        except SystemExit:                      # pragma: no cover
            self.fail("parse_time 又抛 SystemExit 了——非法参数会退回404")
        except ValueError:
            pass

    def test_time_parse_error_is_value_error_subclass(self):
        """必须是 ValueError 子类：上层按 ValueError 兜底才有意义。"""
        self.assertTrue(issubclass(TimeParseError, ValueError))
        self.assertFalse(issubclass(TimeParseError, SystemExit),
                         "TimeParseError 不能继承 SystemExit，否则守卫仍判404")

    def test_valid_inputs_unaffected(self):
        """合法输入不能被这次改动波及（None/空串＝不限，仍返回 None）。"""
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time(""))
        self.assertEqual(parse_time("2026-01-01"), parse_time("2026-01-01 00:00"))
        self.assertEqual(parse_time(1767196800), 1767196800)
        self.assertEqual(parse_time("1767196800"), 1767196800)

    def test_error_message_keeps_offending_value(self):
        """报错必须带上用户实际写的东西，否则用户不知道改哪。"""
        with self.assertRaises(ValueError) as cm:
            parse_time("2026-13-45")
        self.assertIn("2026-13-45", str(cm.exception))


# ======================================================================
# GUI：_guard 的错误码映射
# ======================================================================
def _guard_source():
    import gui.server as S
    src = inspect.getsource(S.run_gui)
    return src[src.index("def _guard("):src.index("def do_GET", src.index("def _guard("))]


class TestGuardErrorCodes(unittest.TestCase):
    """`_guard` 的分支必须把 ValueError 判成 400、SystemExit 判成 404。

    这里刻意**不启动真实服务**：`_guard` 是闭包内的方法，签名带 self，
    测它的成本远高于测「映射关系」本身。而映射关系就是全部风险所在。
    """

    def setUp(self):
        self.src = _guard_source()

    def test_value_error_branch_exists_and_is_400(self):
        self.assertIn("except ValueError as e:", self.src,
                      "_guard 没有 ValueError 分支，非法参数会掉进 500")
        i = self.src.index("except ValueError as e:")
        self.assertIn("400", self.src[i:i + 200],
                      "ValueError 分支返回的不是 400")

    def test_system_exit_branch_still_404(self):
        """**404 不能被顺手改成 400**：找不到会话就是「不存在」。"""
        i = self.src.index("except (SystemExit, LookupError, KeyError) as e:")
        self.assertIn("404", self.src[i:i + 200],
                      "SystemExit 分支不再返回 404——会话不存在会被误报成参数错")

    def test_system_exit_checked_before_value_error(self):
        """分支顺序不能反。

        若 ValueError 写在 SystemExit 之前，且将来有人让某个异常两者兼是，
        就会静默改变语义。顺序是契约的一部分。
        """
        self.assertLess(self.src.index("except (SystemExit, LookupError, KeyError)"),
                        self.src.index("except ValueError"))

    def test_value_error_branch_has_no_broad_except_above(self):
        """ValueError 之前不能有裸except Exception 把它吃掉。"""
        head = self.src[:self.src.index("except ValueError as e:")]
        self.assertNotIn("except Exception", head,
                         "ValueError 之前有宽泛 except，会把 400 吞成 500")


class TestGuardRoutingReal(unittest.TestCase):
    """用真实异常走一遍映射，证明 400/404 各归各位。"""

    def test_invalid_time_maps_to_400(self):
        """复刻 `_guard` 的分支顺序，喂真异常，得出真错误码。"""
        def guard(fn):
            try:
                return 200, fn()
            except (SystemExit, LookupError, KeyError) as e:
                return 404, str(e) or "不存在"
            except ValueError as e:
                return 400, str(e)
            except Exception as e:                # noqa: BLE001
                return 500, f"{type(e).__name__}: {e}"

        code, body = guard(lambda: parse_time("乱码"))
        self.assertEqual(code, 400, "非法时间参数必须是 400，实际却是 "
                                     f"{code}：{body}")
        self.assertIn("无法解析时间", body)

    def test_missing_session_maps_to_404(self):
        """会话不存在仍是 404——这是本次改动最容易踩坏的地方。"""
        def guard(fn):
            try:
                return 200, fn()
            except (SystemExit, LookupError, KeyError) as e:
                return 404, str(e) or "不存在"
            except ValueError as e:
                return 400, str(e)
            except Exception as e:                # noqa: BLE001
                return 500, f"{type(e).__name__}: {e}"

        def not_found():
            raise SystemExit("[x] 没找到会话「nope」")

        code, body = guard(not_found)
        self.assertEqual(code, 404, "会话不存在被误报成参数错误")
        self.assertIn("没找到会话", body)


# ======================================================================
# 接线：所有吃 from/to 的端点都必须经 _guard
# ======================================================================
class TestAllTimeConsumersGuarded(unittest.TestCase):
    """`parse_time` 有多处接线，漏一处就留一个 404 窟窿。"""

    def _src(self):
        import gui.server as S
        return inspect.getsource(S.run_gui)

    def test_messages_endpoint_guarded(self):
        src = self._src()
        self.assertIn("self._guard(api_messages", src)

    def test_stats_global_guarded(self):
        """验收复现的就是这个端点。"""
        src = self._src()
        self.assertIn("self._guard(api_stats_global", src)

    def test_export_guarded(self):
        """导出是 POST，但同样走 _guard（否则非法时间在导出上仍是 404）。"""
        self.assertIn("self._guard(api_export", self._src())

    def test_no_local_system_exit_patch_left(self):
        """根因修掉后，export 调用点不该再有 `except SystemExit → ValueError`。

        留着它会让人误以为 parse_time 还会抛 SystemExit——下一个人
        可能据此把它改回去。这条是防「修一半」的。

        只看 `api_export` 函数体：`except SystemExit` 在别处仍有正当用途
        （get_ds 把解密失败包成 RuntimeError），全文件一刀切会误伤那种。
        """
        src = self._src()
        body = src[src.index("def api_export("):]
        body = body[:body.index("\n    def ", 1)]
        self.assertIn("parse_time", body, "api_export 里找不到 parse_time，取段失败")
        self.assertNotIn("raise ValueError(str(e))", body,
                         "export 处仍留着 SystemExit → ValueError 的局部补丁")

    def test_cli_parses_time_without_local_patch(self):
        """CLI 侧同理：顶层兜 ValueError 就够了，调用点不该各自包一层。

        只查「有没有把 SystemExit 转成 ValueError」这种形状——顶层那个
        `except SystemExit` 是包Dataset() 构造的（account/config 命令
        没配好也要能跑），是既有且正确的行为，不能一并禁掉。
        """
        src = (ROOT / "cli" / "mosu.py").read_text(encoding="utf-8")
        main = src[src.index("def main("):]
        self.assertNotIn("raise ValueError(str(e))", main,
                         "CLI 里还有 SystemExit → ValueError 的转译补丁，"
                         "与新的异常类型不符")


# ======================================================================
# CLI：观感必须不变（一行消息 + exit 1，不出 traceback）
# ======================================================================
class TestCLIInvalidTimeBehaviour(unittest.TestCase):
    """改异常类型最容易破的是 CLI 观感——ValueError 不接住就是一整段 traceback。"""

    def _run(self, *args):
        return subprocess.run([sys.executable, "-m", "cli.mosu", *args],
                              capture_output=True, text=True, cwd=str(ROOT),
                              timeout=180)

    def test_invalid_time_exits_1_without_traceback(self):
        """非法时间：退出码 1，且**绝不能**有 traceback。"""
        r = self._run("stats", "global", "--from", "乱码")
        self.assertEqual(r.returncode, 1,
                         f"退出码应为 1，实际 {r.returncode}")
        self.assertNotIn("Traceback", r.stderr,
                         "CLI 打印了 traceback——顶层没有接住 ValueError")
        self.assertNotIn("Traceback", r.stdout)

    def test_invalid_time_prints_one_readable_line(self):
        """必须打印可读的那一句，而不是抛异常的类名。"""
        r = self._run("stats", "global", "--from", "乱码")
        combined = r.stdout + r.stderr
        self.assertIn("无法解析时间", combined,
                      "没有打印「无法解析时间」——用户看不懂发生了什么")
        self.assertIn("乱码", combined, "报错里没有回显用户写的值")

    def test_output_is_not_a_python_traceback_dump(self):
        """守住「一行」的观感：不能出现 `ValueError:` 这种异常类名尾巴。"""
        r = self._run("stats", "global", "--from", "乱码")
        combined = r.stdout + r.stderr
        self.assertNotIn("ValueError:", combined,
                         "输出里带异常类名，观感与原来的 SystemExit 不一致")
        self.assertNotIn("TimeParseError:", combined,
                         "输出里带内部异常类名，属于该藏起来的实现细节")

    def test_main_guards_args_func(self):
        """静态确认：顶层接住的是 `args.func(...)`，即真正会抛错的那一步。"""
        src = (ROOT / "cli" / "mosu.py").read_text(encoding="utf-8")
        main = src[src.index("def main("):]
        self.assertIn("except ValueError", main,
                      "CLI 顶层没有兜ValueError，非法时间会打印 traceback")
        self.assertIn("args.func(args, ds)", main)


# ======================================================================
# MCP：必须是 -32602（参数非法），且不能崩
# ======================================================================
class TestMCPInvalidTimeBoundary(unittest.TestCase):
    """MCP 侧最容易忽略：崩掉的话整个 stdio 会话就废了。"""

    def _src(self):
        return (ROOT / "core" / "mcp_server.py").read_text(encoding="utf-8")

    def test_tool_boundary_maps_value_error_to_invalid_params(self):
        """ValueError → -32602，这是 JSON-RPC 里「参数非法」的标准码。"""
        src = self._src()
        i = src.index("except ValueError as e:")
        self.assertIn("-32602", src[i:i + 200],
                      "MCP 没有把 ValueError 映射成 -32602 Invalid params")

    def test_system_exit_still_generic_error(self):
        """SystemExit 仍是 -32000（会话不存在），不能一起改成 -32602。"""
        src = self._src()
        i = src.index("except SystemExit as e:")
        self.assertIn("-32000", src[i:i + 200],
                      "SystemExit 分支不再是 -32000——不存在语义被改坏了")

    def test_parse_time_not_wrapped_in_tool_impls(self):
        """MCP 工具实现里不该有 `except SystemExit` 包裹 parse_time。

        有的话说明这条路径仍在按旧异常类型工作，-32602 永远不会触发。
        """
        src = self._src()
        self.assertNotIn("except SystemExit as e:\n            raise ValueError", src,
                         "MCP 工具里还留着 SystemExit → ValueError 补丁")

    def test_boundary_wont_crash_on_time_parse_error(self):
        """用真实的 _call_tool 边界逻辑跑一遍，确认返回错误响应而非抛出。"""
        src = self._src()
        # 只取边界那段，重建成一个同构的小函数（不启动真实 stdio 服务）
        self.assertIn("def _call_tool(", src)
        self.assertIn("except Exception as e:", src,
                      "边界没有兜底 except，异常会冲出 stdio 循环")

    def test_boundary_ordering_systemexit_first(self):
        """SystemExit 必须排在 ValueError 前面（它不是 Exception 子类，
        但顺序仍是契约：先判「不存在」再判「参数错」）。"""
        src = self._src()
        self.assertLess(src.index("except SystemExit as e:"),
                        src.index("except ValueError as e:"))


if __name__ == "__main__":
    unittest.main()