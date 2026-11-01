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


class TestExportNoOverwrite(unittest.TestCase):
    """单会话导 html 不能写同一个 index.html。

    ChatExporter 里路径是写死的 `out_dir / "index.html"`。
    如果单会话也直接用 export/ 根目录，导完 A 再导 B，
    A 的 index.html 就被 B 覆盖了 —— 这是无声的数据丢失。
    修法是按会话名分目录（export/<会话名>/index.html）。
    """

    def test_html_out_dir_is_per_session(self):
        src = SERVER.read_text(encoding="utf-8")
        # 不能再有「multi 才分目录」的分支
        self.assertNotIn("out_root / name if multi else out_root", src,
                         "html 导出又回到「单会话写根目录」了，会互相覆盖")
        self.assertIn("out_dir = out_root / name", src,
                      "html 导出没按会话名分目录")

    def test_exporter_still_writes_index_html(self):
        """确认上游契约没变：ChatExporter 依旧是写 index.html。

        如果哪天它改成可传文件名，这条会红，提醒同步改注释与测试。
        """
        exp = (ROOT / "core" / "exporter.py").read_text(encoding="utf-8")
        self.assertIn('out = self.out_dir / "index.html"', exp)


class TestUnknownParamsRejected(unittest.TestCase):
    """传了 schema 里没有的参数，必须报错而不是静默忽略。

    实测踩过：矩阵里有 7 个用例参数名写错（author vs user、kind vs year、
    from/to 压根不存在…），工具不报错、把参数忽略掉，用例一路绿灯 ——
    **测了等于没测，比不测更危险**，它给了虚假的安全感。

    对模型来说同理：它传错参数名时拿到的是全量数据，
    却以为过滤生效了，最后给用户的结论就是错的。
    """

    def setUp(self):
        from core.mcp_server import McpServer
        self.validate = McpServer._validate

    def test_unknown_param_lists_available_ones(self):
        with self.assertRaises(ValueError) as cm:
            self.validate("mosu_moments", {"author": "某人"})
        msg = str(cm.exception)
        self.assertIn("author", msg)
        self.assertIn("可用参数", msg)
        self.assertIn("user", msg)      # 正确的那个要出现在提示里

    def test_account_is_never_unknown(self):
        """account 由 call() 统一消费，任何工具都该接受。"""
        self.validate("mosu_status", {"account": "wxid_a"})
        self.validate("mosu_media", {"md5": "0" * 32, "account": "wxid_a"})


class TestParseTimeEnd(unittest.TestCase):
    """`to` 只给日期时，必须补到当天 23:59:59。

    否则用户问「10 月的消息」，传 to=2026-10-31，
    解析成 10-31 00:00 —— 整天被漏掉。
    """

    def test_end_of_day_is_filled(self):
        import datetime
        from core.dataset import parse_time
        t = parse_time("2026-10-31", end=True)
        dt = datetime.datetime.fromtimestamp(t)
        self.assertEqual((dt.hour, dt.minute, dt.second), (23, 59, 59))
        self.assertEqual((dt.month, dt.day), (10, 31))

    def test_start_of_day_unchanged(self):
        import datetime
        from core.dataset import parse_time
        t = parse_time("2026-10-01")
        dt = datetime.datetime.fromtimestamp(t)
        self.assertEqual((dt.hour, dt.minute, dt.second), (0, 0, 0))

    def test_explicit_time_not_overridden(self):
        """给了具体时间的，end=True 也不该改写它。"""
        import datetime
        from core.dataset import parse_time
        t = parse_time("2026-10-31 12:30", end=True)
        dt = datetime.datetime.fromtimestamp(t)
        self.assertEqual((dt.hour, dt.minute), (12, 30))

    def test_all_tools_use_end_for_to(self):
        """所有工具的 to 都必须带 end=True，否则会漏最后一天。"""
        src = SERVER.read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if 'parse_time(a.get("to"))' in line:
                bad.append(line.strip())
        self.assertFalse(bad, f"这些 to 没用 end=True，会漏掉最后一天：{bad}")


class TestSearchTimeRangeDeclared(unittest.TestCase):
    """mosu_search 要能按时间范围搜（底层 ds.search 本来就支持）。"""

    def test_from_to_are_declared(self):
        s = next(t for t in _tools() if t["name"] == "mosu_search")
        props = (s["inputSchema"] or {}).get("properties") or {}
        self.assertIn("from", props, "search 缺 from 参数")
        self.assertIn("to", props, "search 缺 to 参数")


class TestProfileTimeRangeSQLParams(unittest.TestCase):
    """按时间范围做画像时，SQL 参数顺序必须对。

    实测 bug（2026-11-02，最隐蔽的一个）：
        core/profile.py 的 _sql_aggregate 里 SQL 是
            SELECT count(*), SUM(CASE WHEN real_sender_id=? ...) FROM t WHERE create_time>=? AND <=?
        但参数传的是 `params + [sid]`，即 [from_ts, to_ts, sid]。
        `?` 按出现顺序绑定 → real_sender_id 拿到 from_ts，
        create_time 拿到 to_ts 和 sid → **查出来恒为 0**。

    症状：问「我 10 月发了多少条」，工具答「0 条」。
    而且不报错、不崩溃，模型只能猜「可能是数据源问题」——
    用户拿到的是一个看起来合理、实际全错的答案。

    为什么一直没发现：不带时间范围时 params 为空，[sid] 刚好对上。
    """

    @classmethod
    def setUpClass(cls):
        # conftest 把 MOSU_HOME 指到临时目录（隔离真实数据），
        # 所以要跑「按真实数据验正确性」的测试必须显式开闸：
        #     MOSU_ALLOW_REAL_DATA=1 python -m pytest tests/test_mcp_schema.py
        # 默认跳过 —— 尊重既有的测试隔离，不让单测悄悄读用户真实聊天记录。
        import json
        import os
        if os.environ.get("MOSU_ALLOW_REAL_DATA") != "1":
            raise unittest.SkipTest("需要真实数据：设 MOSU_ALLOW_REAL_DATA=1 再跑")
        # 绕开被 conftest 改掉的 MOSU_HOME，直接读真实密钥库
        cfg_path = Path.home() / ".mosu" / "config.json"
        if not cfg_path.exists():
            raise unittest.SkipTest("没有 ~/.mosu/config.json")
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cur = cfg.get("current") or ""
            acc = (cfg.get("accounts") or {}).get(cur) or {}
            dbp = acc.get("dbPath")
            key = acc.get("keyHex")
            if not (dbp and key):
                raise unittest.SkipTest("真实配置里缺 dbPath/keyHex")
            from core.dataset import Dataset
            cls.ds = Dataset(data_dir=dbp, key_hex=key, quiet=True)
        except unittest.SkipTest:
            raise
        except Exception as e:  # noqa: BLE001
            raise unittest.SkipTest(f"读真实数据失败：{e}")
        if not cls.ds.sessions():
            raise unittest.SkipTest("没有会话数据")

    def test_range_profile_is_not_zero(self):
        """带时间范围的画像必须有数据，不能恒为 0。"""
        from core.dataset import parse_time
        from core.profile import ProfileDistiller

        f = parse_time("2026-01-01")
        t = parse_time("2026-12-31", end=True)
        p = ProfileDistiller(self.ds).self_profile(from_ts=f, to_ts=t)
        ov = p["overview"]
        self.assertGreater(ov["totalMessages"], 0,
                           "带时间范围的画像算出 0 条 —— SQL 参数顺序大概又错了")
        self.assertGreater(ov["myMessages"], 0,
                           "「我发出的」算出 0 条 —— 检查 real_sender_id 的绑定")

    def test_range_subset_of_all(self):
        """区间内的量必须小于等于全量，且两段区间相加不超过全量。"""
        from core.dataset import parse_time
        from core.profile import ProfileDistiller
        pd = ProfileDistiller(self.ds)
        alln = pd.self_profile()["overview"]["totalMessages"]
        p1 = pd.self_profile(from_ts=parse_time("2026-01-01"),
                             to_ts=parse_time("2026-06-30", end=True))
        p2 = pd.self_profile(from_ts=parse_time("2026-07-01"),
                             to_ts=parse_time("2026-12-31", end=True))
        s = p1["overview"]["totalMessages"] + p2["overview"]["totalMessages"]
        self.assertLessEqual(s, alln, "两段区间之和超过了全量，时间过滤有问题")
        self.assertGreater(s, 0, "两段区间都是 0")

    def test_sid_bound_before_time_params(self):
        """源码断言：sid 必须排在时间参数前面（防止有人改回去）。"""
        src = (ROOT / "core" / "profile.py").read_text(encoding="utf-8")
        self.assertNotIn("params + [sid]", src,
                         "SQL 参数又变回 params + [sid] 了 —— "
                         "SELECT 里的 ? 在 WHERE 之前，sid 必须先绑定")
