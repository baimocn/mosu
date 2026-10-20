# -*- coding: utf-8 -*-
"""GUI HTTP 接口契约测试（真发请求，不用 mock）。

## 为什么要有这一组

`gui/server.py` 的 `do_GET` 是一条 183 行 / 26 分支的 if 链。
想重构它（改成路由表），必须先有东西能证明"重构前后行为一致"——
否则改完只能靠手点，那等于没有回归保护。

这组测试故意只断言**契约**（状态码、关键字段类型、错误时的行为），
不锁具体数值——数值天天变，锁死了会天天假红。

需要 GUI 服务在跑；没跑则整组跳过。
"""
import json
import os
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = os.environ.get("MOSU_GUI_BASE", "http://127.0.0.1:8788")

# 本机有系统代理，127.0.0.1 会被劫持成 502；必须显式绕开
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _alive() -> bool:
    try:
        with _OPENER.open(f"{BASE}/api/status", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def get(path: str, **params):
    """返回 (status, payload)。payload 尽力解析为 JSON。"""
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with _OPENER.open(url, timeout=60) as r:
            body = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        # 必须显式 close：HTTPError 自带一个未读的响应体，
        # 不关会触发 ResourceWarning（测试输出会变脏，掩盖真的警告）
        try:
            body = e.read()
        finally:
            e.close()
        code = e.code
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}
    try:
        return code, json.loads(body)
    except Exception:
        return code, {"_raw": body[:200]}


@unittest.skipUnless(_alive(), f"需要 GUI 服务在 {BASE}")
class TestRouteContract(unittest.TestCase):
    """所有 GET 路由的基本契约：不是 500、返回 JSON。"""

    ROUTES = [
        ("/api/status", {}),
        ("/api/log", {"n": 5}),
        ("/api/sessions", {"limit": 5}),
        ("/api/contacts", {"limit": 5}),
        ("/api/accounts", {}),
        ("/api/config", {}),
        ("/api/personas", {}),
        ("/api/llm", {}),
        ("/api/stats/global", {}),
        ("/api/stats/contacts", {"top": 5}),
        ("/api/stats/time", {"by": "hour"}),
    ]

    def test_routes_do_not_500(self):
        """任何路由返回 500 都是缺陷——500 意味着异常没被接住。"""
        for path, params in self.ROUTES:
            with self.subTest(path=path):
                code, body = get(path, **params)
                self.assertNotEqual(code, 500, f"{path} 返回 500: {body}")
                self.assertEqual(code, 200, f"{path} 状态码异常: {code} {body}")
                self.assertIsInstance(body, dict, f"{path} 未返回 JSON 对象")

    def test_unknown_route_is_404_json(self):
        """未知路径要返回 404 JSON，不能是 HTML 或空响应——
        前端所有请求都按 JSON 解析，拿到 HTML 会抛解析错误。"""
        code, body = get("/api/definitely-not-exist")
        self.assertEqual(code, 404)
        self.assertIn("error", body)


class TestStatusContract(unittest.TestCase):
    @unittest.skipUnless(_alive(), f"需要 GUI 服务在 {BASE}")
    def test_status_shape(self):
        """status 是前端启动依赖的第一个接口，字段缺失会导致整页卡在骨架。"""
        code, d = get("/api/status")
        self.assertEqual(code, 200)
        for k in ("wechat", "service", "bot", "data"):
            self.assertIn(k, d, f"status 缺少 {k}")
        self.assertIn("logged_in", d["wechat"])
        self.assertIn("alive", d["service"])


class TestSessionsContract(unittest.TestCase):
    @unittest.skipUnless(_alive(), f"需要 GUI 服务在 {BASE}")
    def test_sessions_shape(self):
        code, d = get("/api/sessions", limit=3)
        self.assertEqual(code, 200)
        self.assertIn("sessions", d)
        self.assertIn("total", d)
        if d["sessions"]:
            s = d["sessions"][0]
            for k in ("sessionId", "displayName", "type", "messageCount"):
                self.assertIn(k, s, f"会话缺少字段 {k}")

    def test_limit_respected(self):
        if not _alive():
            self.skipTest("服务未启动")
        code, d = get("/api/sessions", limit=2)
        self.assertLessEqual(len(d.get("sessions") or []), 2)

    def test_invalid_limit_clamped(self):
        """limit 传 0 或超大值不能让服务崩，要被夹到合法区间。"""
        if not _alive():
            self.skipTest("服务未启动")
        for bad in (0, -5, 99999):
            code, d = get("/api/sessions", limit=bad)
            self.assertEqual(code, 200, f"limit={bad} 导致 {code}")


class TestSearchContract(unittest.TestCase):
    @unittest.skipUnless(_alive(), f"需要 GUI 服务在 {BASE}")
    def test_empty_query(self):
        code, d = get("/api/search", q="")
        self.assertEqual(code, 200)
        self.assertEqual(d.get("total"), 0)
        self.assertEqual(d.get("messages"), [])

    def test_pagination_fields(self):
        code, d = get("/api/search", q="的", limit=5)
        self.assertEqual(code, 200)
        for k in ("total", "offset", "hasMore", "messages"):
            self.assertIn(k, d, f"搜索缺少 {k}")


class TestMediaContract(unittest.TestCase):
    @unittest.skipUnless(_alive(), f"需要 GUI 服务在 {BASE}")
    def test_bad_md5_rejected(self):
        """md5 不是 32 位要明确拒绝，不能拿去做文件查找。"""
        code, d = get("/api/media", md5="abc")
        self.assertIn(code, (400, 404))
        self.assertIn("error", d)

    def test_missing_image_has_reason(self):
        """取不到图时要说明原因（没记录 / 未下载占位 / 解码失败），
        只回一句"找不到"用户不知道该不该去微信里刷一次。"""
        code, d = get("/api/media", md5="0" * 32)
        self.assertIn(code, (400, 404))
        msg = d.get("error", "")
        self.assertTrue(len(msg) > 5, f"错误信息太笼统: {msg!r}")


class TestParamValidation(unittest.TestCase):
    """参数校验：非法输入应该 4xx，不该 500，也不该静默接受。"""

    @unittest.skipUnless(_alive(), f"需要 GUI 服务在 {BASE}")
    def test_contact_requires_wxid(self):
        code, d = get("/api/contact")
        self.assertEqual(code, 400)
        self.assertIn("error", d)

    def test_stats_time_bad_by(self):
        code, d = get("/api/stats/time", by="nonsense")
        self.assertNotEqual(code, 500, f"非法 by 参数导致 500: {d}")


if __name__ == "__main__":
    unittest.main()
