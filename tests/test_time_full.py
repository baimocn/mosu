# -*- coding: utf-8 -*-
"""消息气泡的时间必须精确到秒、带年份与时区（可逐条核对 epoch）。

历史缺陷：core.js 的 fmtTime 只显示到分钟，跨天显示「11月9日 23:17」——
数值其实没错，但用户没法拿它跟数据库里的 createTime 对上，
同一分钟内多条消息也分不出先后。fmtTimeFull 专门给消息气泡用，
这里用真实 node 跑真实 JS，锁死输出格式。
"""
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_JS = ROOT / "gui" / "assets" / "core.js"

NODE = shutil.which("node")
if not NODE:
    for cand in (os.environ.get("NODE_HOME") or "",
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs"),
                 r"C:\Program Files\nodejs"):
        if cand and (Path(cand) / "node.exe").exists():
            NODE = str(Path(cand) / "node.exe")
            break


@unittest.skipUnless(NODE, "node 不可用，跳过前端 JS 断言")
class TestFmtTimeFull(unittest.TestCase):
    def setUp(self):
        # core.js 是 IIFE，依赖 window/Vue/localStorage/matchMedia。
        # 造最小替身，只为拿到挂在 window.Mosu 上的 fmtTimeFull。
        src = CORE_JS.read_text(encoding="utf-8")
        harness = """
const Vue = { reactive: (x) => x, ref: (x) => ({ value: x }) };
const localStorage = { getItem: () => null, setItem: () => {} };
const window = {
  Mosu: {}, matchMedia: () => ({ matches: false, addEventListener: () => {} }),
};
""" + src + """
const ts = Number(process.env.MOSU_TEST_TS || 0);
process.stdout.write(JSON.stringify({
  full: window.Mosu.fmtTimeFull(ts),
  short: window.Mosu.fmtTime(ts),
}));
"""
        self.harness = harness

    def _run(self, ts):
        env = dict(os.environ, MOSU_TEST_TS=str(ts), TZ="Asia/Shanghai")
        out = subprocess.run([NODE, "-e", self.harness],
                             capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_full_has_seconds_year_timezone(self):
        # 1794237438 = 2026-11-09 23:17:18 +08:00（与真实消息一致）
        r = self._run(1794237438)
        self.assertEqual(r["full"], "2026-11-09 23:17:18 (UTC+08:00)")
        # 简写仍是分钟级，两种格式刻意共存
        self.assertNotIn(":18", r["short"])

    def test_full_distinguishes_same_minute(self):
        """同一分钟内的两条消息，full 必须不相等（这正是精度要解决的问题）。"""
        a = self._run(1794237438)["full"]
        b = self._run(1794237449)["full"]
        self.assertNotEqual(a, b)
        self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \(UTC[+-]\d{2}:\d{2}\)$", a))

    def test_empty_timestamp(self):
        self.assertEqual(self._run(0)["full"], "")


if __name__ == "__main__":
    unittest.main()
