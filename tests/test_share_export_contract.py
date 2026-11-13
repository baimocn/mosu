# -*- coding: utf-8 -*-
"""v2 分享页与 /api/export、/api/exports 的字段契约。

历史缺陷（第三次同源错配）：前端把 form.value 原样 POST，字段叫 sessionId，
后端契约是 session → 用户点导出必然收到「缺少 session（会话 wxid）」。
批量导出传 batch/count，后端认的是 scope:"all"/topN。
导出是异步任务，返回 {taskId,status:"running"}，不能当同步结果直接报完成。
历史列表读顶层 name，但后端给的是 files[].name。

这里用真实源码文本锁住这些契约，避免 v2 迁移再漏。
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARE = (ROOT / "gui" / "assets" / "v2" / "views-share.js").read_text(encoding="utf-8")
SERVER = (ROOT / "gui" / "server.py").read_text(encoding="utf-8")


class TestExportRequestContract(unittest.TestCase):
    def test_backend_requires_session(self):
        self.assertIn('payload.get("session")', SERVER)
        self.assertIn('缺少 session（会话 wxid）', SERVER)

    def test_frontend_sends_session_not_session_id(self):
        self.assertIn("session: form.value.sessionId", SHARE)
        # 不能把整个 form 原样 POST
        self.assertNotIn("Object.assign({}, form.value)", SHARE)

    def test_frontend_waits_for_async_task(self):
        self.assertIn("waitTask(d.taskId)", SHARE)
        self.assertIn('/api/task', SHARE)
        self.assertIn('t.status === "done"', SHARE)

    def test_batch_uses_scope_all_and_topn(self):
        self.assertIn('scope: "all"', SHARE)
        self.assertIn("topN: batch.value.count", SHARE)
        self.assertNotIn("batch: true", SHARE)

    def test_history_reads_files_name(self):
        self.assertIn("x.files && x.files[0]", SHARE)
        self.assertIn("f.name", SHARE)


class TestBackendAsyncContract(unittest.TestCase):
    def test_export_returns_task_id(self):
        self.assertIn('"taskId": tid', SERVER)
        self.assertIn('"status": "running"', SERVER)


if __name__ == "__main__":
    unittest.main()
