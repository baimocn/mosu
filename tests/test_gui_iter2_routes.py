# -*- coding: utf-8 -*-
"""迭代 2 的 GUI 接口契约测试（SSE 事件流 + 运行历史）。

跟 test_gui_routes.py 同一套路：真发请求，不用 mock；服务没跑就跳过。

要锁的契约：
  - /api/runs 返回摘要列表，字段名带 Count 后缀（不与详情里的数组字段同名）
  - /api/runs/detail 对非法 id 返回 400/404 而不是 500
  - /api/runs/detail 防路径穿越（run_id 来自 URL query）
  - /api/insight/stream 对不存在的 task 返回 error 事件而不是挂死
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
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _alive() -> bool:
    try:
        with _OPENER.open(f"{BASE}/api/status", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def get(path: str, **params):
    """返回 (status, payload)。"""
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with _OPENER.open(url, timeout=60) as r:
            body = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
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


def read_sse(path: str, timeout=20, max_events=50):
    """读 SSE 流，返回事件列表。每个事件 {"name":..., "data":...}。"""
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    events = []
    buf = b""
    with _OPENER.open(req, timeout=timeout) as r:
        while len(events) < max_events:
            chunk = r.read1(4096) if hasattr(r, "read1") else r.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                text = raw.decode("utf-8", "replace").strip()
                if not text or text.startswith(":"):
                    continue
                name, data = "message", {}
                for line in text.split("\n"):
                    if line.startswith("event: "):
                        name = line[7:].strip()
                    elif line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                        except ValueError:
                            data = {"raw": line[6:]}
                events.append({"name": name, "data": data})
                if name in ("done", "error", "end"):
                    return events
    return events


@unittest.skipUnless(_alive(), f"需要 GUI 服务在 {BASE}")
class TestRunsContract(unittest.TestCase):
    """运行历史接口契约。"""

    def test_runs_returns_list(self):
        code, d = get("/api/runs")
        self.assertEqual(code, 200, d)
        self.assertIn("runs", d)
        self.assertIsInstance(d["runs"], list)

    def test_summary_uses_count_suffix(self):
        """计数字段必须带 Count 后缀。

        摘要里的 evidence 是整数、详情里的 evidence 是数组——同名不同型
        是最容易埋雷的写法，前端拿错不报错，只渲染出空列表。
        """
        code, d = get("/api/runs")
        self.assertEqual(code, 200)
        for item in d.get("runs") or []:
            for k in ("steps", "evidence", "branches"):
                self.assertNotIn(k, item,
                                 f"摘要里不该有裸字段 {k}（应叫 {k}Count）")
            for k in ("stepsCount", "evidenceCount", "branchesCount"):
                self.assertIn(k, item)
                self.assertIsInstance(item[k], int)

    def test_summary_has_required_fields(self):
        code, d = get("/api/runs")
        self.assertEqual(code, 200)
        for item in d.get("runs") or []:
            for k in ("id", "question", "createdAt"):
                self.assertIn(k, item, f"摘要缺少 {k}")

    def test_detail_missing_id_is_400(self):
        code, d = get("/api/runs/detail")
        self.assertEqual(code, 400, d)
        self.assertIn("error", d)

    def test_detail_unknown_id_is_404(self):
        code, d = get("/api/runs/detail", id="20990101-000000-deadbeef")
        self.assertEqual(code, 404, d)
        self.assertIn("error", d)

    def test_detail_rejects_path_traversal(self):
        """run_id 来自 URL query —— 不防穿越就是任意文件读。"""
        for bad in ("../../etc/passwd", "..%5C..%5Cwindows", "....//....//x"):
            code, d = get("/api/runs/detail", id=bad)
            self.assertNotEqual(code, 200, f"{bad} 居然读成功了")
            self.assertIn(code, (400, 404), f"{bad} → {code} {d}")

    def test_delete_unknown_id_does_not_500(self):
        code, d = get("/api/runs/delete", id="20990101-000000-deadbeef")
        self.assertNotEqual(code, 500, d)
        self.assertIn("deleted", d)


@unittest.skipUnless(_alive(), f"需要 GUI 服务在 {BASE}")
class TestInsightStreamContract(unittest.TestCase):
    """SSE 事件流契约。"""

    def test_unknown_task_returns_error_event(self):
        """不存在的 task 要回 error 事件并结束，不能挂死。

        挂死的后果很严重：SSE 线程会一直占着，客户端一直等。
        """
        events = read_sse("/api/insight/stream?id=nosuchtask123", timeout=15)
        self.assertTrue(events, "一个事件都没收到——服务端可能挂住了")
        names = [e["name"] for e in events]
        self.assertIn("error", names, f"没有 error 事件：{names}")

    def test_missing_id_returns_error(self):
        events = read_sse("/api/insight/stream", timeout=15)
        self.assertTrue(events)
        self.assertIn("error", [e["name"] for e in events])

    def test_error_event_carries_message(self):
        events = read_sse("/api/insight/stream?id=nosuchtask123", timeout=15)
        errs = [e for e in events if e["name"] == "error"]
        self.assertTrue(errs)
        self.assertIn("error", errs[0]["data"])


class TestStaticWiring(unittest.TestCase):
    """不依赖 GUI 服务的静态接线检查。

    为什么要这一组：上面的契约测试**服务没跑就整组跳过**——CI 里等于没有。
    这组用源码静态检查锁住关键接线（路由注册了、前端调了、SSE 端点存在），
    任何环境都能跑。
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.server = (root / "gui" / "server.py").read_text(encoding="utf-8")
        cls.insight_js = (root / "gui" / "assets" / "v2" / "views-insight.js") \
            .read_text(encoding="utf-8")

    def test_server_has_runs_endpoints(self):
        for p in ("/api/runs", "/api/runs/detail", "/api/runs/delete",
                  "/api/insight/stream"):
            self.assertIn(p, self.server, f"服务端缺少路由 {p}")

    def test_server_has_stream_function(self):
        self.assertIn("def api_insight_stream", self.server)

    def test_stream_sends_snapshot_before_deltas(self):
        """SSE 必须**先发快照再发增量**。

        不先发快照 → 订阅建立前发生的步骤会丢，用户看到的轨迹凭空少开头。
        """
        i = self.server.index("def api_insight_stream")
        body = self.server[i:i + 4000]
        self.assertIn('emit("snapshot"', body, "SSE 没有发快照")
        self.assertIn('emit("delta"', body, "SSE 没有发增量事件")
        self.assertLess(body.index('emit("snapshot"'), body.index('emit("delta"'),
                        "快照必须在增量之前发")

    def test_server_uses_condition_wait_correctly(self):
        """wait_events 必须先查条件再等——直接 wait 会丢唤醒。

        丢唤醒的后果：每个事件都被延迟满一个 timeout（实测 1 秒）。
        """
        i = self.server.index("def wait_events")
        body = self.server[i:i + 2000]
        self.assertIn("while True", body, "wait_events 没有重查条件")
        self.assertIn("self._cond.wait(", body)
        # 不能是「一进来就 wait」——那正是丢唤醒的写法
        first_wait = body.index("self._cond.wait(")
        self.assertLess(body.index("if evs:"), first_wait,
                        "wait 之前必须先检查条件（否则丢唤醒）")

    def test_taskhub_has_event_sequence(self):
        for frag in ("def emit(", "def wait_events(", "def snapshot(",
                     "_events", "_seq"):
            self.assertIn(frag, self.server, f"TaskHub 缺少 {frag}")

    def test_insight_ask_persists_run(self):
        """洞察跑完必须落盘，否则「历史可回看」是空话。"""
        i = self.server.index("def api_insight_ask")
        body = self.server[i:i + 6000]
        self.assertIn("save_run", body, "洞察没有落盘")
        self.assertIn("runs_mod", body)

    def test_frontend_uses_event_source(self):
        self.assertIn("new EventSource", self.insight_js,
                      "前端没有用 SSE（迭代 2 的核心）")
        self.assertIn("/api/insight/stream", self.insight_js)

    def test_frontend_has_poll_fallback(self):
        """必须有轮询降级——SSE 只影响实时性，不能影响正确性。"""
        self.assertIn("pollTask", self.insight_js)
        self.assertIn("/api/task", self.insight_js)

    def test_frontend_calls_history_api(self):
        for frag in ("/api/runs", "/api/runs/detail"):
            self.assertIn(frag, self.insight_js, f"前端没调 {frag}")

    def test_frontend_exposes_history_methods(self):
        """模板里用的方法必须在 setup 里 return，否则运行时 xxx is not a function。"""
        for name in ("toggleHistory", "loadHistory", "openHistory",
                     "delHistory", "fmtTime"):
            self.assertIn(f"function {name}", self.insight_js,
                          f"缺函数 {name}")
            self.assertIn(name, self.insight_js.split("return {")[-1],
                          f"{name} 没在 setup 的 return 里暴露")


if __name__ == "__main__":
    unittest.main()
