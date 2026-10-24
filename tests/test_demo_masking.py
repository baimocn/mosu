# -*- coding: utf-8 -*-
"""演示模式脱敏的回归测试（自起 demo 实例，不依赖外部服务）。

## 为什么要有

2026-10-25 实测：`/api/report` 的 `summary.topContacts[].wxid` 在演示模式下
混着真实 id（wxid_t5hx…、10000000001@chatroom）——demo 数据层换了
displayName，但这个字段走另一条路径不会被换。shots.py 的黑名单截年报页时
抓到，才补上 api_report 的脱敏。本组测试把那条修复钉住：
**演示模式的任何接口都不许吐真实 wxid / 群号 / 本机路径。**

## 两个环境事实（都踩过）

1. demo 模式是「脱敏渲染层」不是「假数据集」——它仍然要读真实数据目录，
   所以子进程必须用**真实的** MOSU_HOME（conftest 把 MOSU_HOME 指到了临时
   空目录，继承过去 demo 服务会因「没找到微信数据目录」直接退出）。
   没有真实配置的机器（CI）整体跳过。
2. 子进程 stdout 走临时文件而不是 PIPE——GUI 服务会往 stdout 写日志，
   PIPE 没人读会写满缓冲区把服务端阻塞死（看着像「起不来」）。
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 本机有系统代理，127.0.0.1 会被劫持成 502；必须显式绕开
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 与 scripts/shots.py 同一份黑名单语义（demo 假 id 形如 wxid_demo1234，
# 不能一刀切禁 "wxid_"——第一版 shots.py 就这样误报过）
FORBIDDEN = ["C:\\Users\\", "D:\\wechat-data", "@im.wechat", "DEMOUSER"]
FORBIDDEN_RE = [
    (re.compile(r"wxid_(?!demo)[a-z0-9]{6,}"), "真实 wxid"),
    (re.compile(r"(?<!demo)\b\d{9,}@chatroom"), "真实群号"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestDemoMasking(unittest.TestCase):
    """对 demo 实例的 /api/report 与 /api/stats/global 做脱敏断言。"""

    @classmethod
    def setUpClass(cls):
        real_home = Path.home() / ".mosu"
        if not (real_home / "config.json").is_file():
            raise unittest.SkipTest("本机无 ~/.mosu/config.json（CI/未配置机器跳过）")
        cls.port = _free_port()
        cls._log = tempfile.NamedTemporaryFile(
            mode="w+", suffix=".log", delete=False, encoding="utf-8")
        env = {**os.environ, "MOSU_HOME": str(real_home)}
        cls.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "demo_server.py"),
             "--port", str(cls.port)],
            stdout=cls._log, stderr=subprocess.STDOUT, env=env)
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + 60          # 首启要解密+建索引，给足
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                cls._log.flush()
                tail = Path(cls._log.name).read_text(encoding="utf-8")[-300:]
                raise unittest.SkipTest(
                    f"demo 实例提前退出 rc={cls.proc.returncode}: {tail}")
            try:
                with _OPENER.open(f"{cls.base}/api/status", timeout=2) as r:
                    st = json.loads(r.read())
                if (st.get("wechat") or {}).get("account") == "演示账号":
                    return
            except Exception:
                time.sleep(0.5)
        raise unittest.SkipTest("demo 实例 60s 内没就绪")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        except Exception:
            cls.proc.kill()
        try:
            cls._log.close()
            os.unlink(cls._log.name)
        except Exception:
            pass

    def _get(self, path: str):
        with _OPENER.open(f"{self.base}{path}", timeout=60) as r:
            return json.loads(r.read())

    def _assert_clean(self, label: str, obj) -> None:
        text = json.dumps(obj, ensure_ascii=False)
        leaks = [f for f in FORBIDDEN if f in text]
        for rx, kind in FORBIDDEN_RE:
            m = rx.search(text)
            if m:
                leaks.append(f"{kind}: {m.group(0)}")
        self.assertEqual(leaks, [], f"{label} 泄漏真实信息")

    def test_report_all_time_clean(self):
        """回归：api_report 的 topContacts.wxid 必须脱敏（2026-10-25 修复）。"""
        self._assert_clean("report(all_time)", self._get("/api/report"))

    def test_report_year_clean(self):
        self._assert_clean("report(year)", self._get("/api/report?year=2026"))

    def test_stats_global_clean(self):
        self._assert_clean("stats/global", self._get("/api/stats/global"))


if __name__ == "__main__":
    unittest.main()
