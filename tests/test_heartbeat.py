# -*- coding: utf-8 -*-
"""心跳状态机的测试。

心跳的作用不是"显示一个绿点"，而是让用户知道**该做什么**。
所以这里测的重点是：不同故障场景能否被区分开。

背景（2026-10-21 实发故障）：心跳停了 15.6 小时，控制台显示"无心跳"，
但当时无法判断是进程死了、还是卡在某次长轮询里——两者处理方式相反：
  - 进程死了 → 拉起
  - 卡在请求里 → 杀掉重来
原来的实现只在 `get_updates()` **成功返回后**才写心跳，长轮询正常也要
挂 20~60 秒，一旦卡死就完全不更新，于是"活着的进程"和"死了的进程"
在外部看起来一模一样。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.server import HEARTBEAT_MAX_AGE, _heartbeat   # noqa: E402


def _fake_hb(payload):
    """让 _heartbeat 读到指定的心跳内容。"""
    return patch("core.wechat._read_json", return_value=payload)


class TestHeartbeatReading(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with _fake_hb({}):
            self.assertEqual(_heartbeat(), {})

    def test_no_ts_returns_empty(self):
        """有文件但没 ts，等同于没有心跳。"""
        with _fake_hb({"phase": "polling"}):
            self.assertEqual(_heartbeat(), {})

    def test_reads_phase_and_age(self):
        now = int(time.time())
        with _fake_hb({"ts": now - 5, "phase": "ok", "got": 3, "pid": 1234}):
            h = _heartbeat()
        self.assertEqual(h["phase"], "ok")
        self.assertEqual(h["got"], 3)
        self.assertEqual(h["pid"], 1234)
        self.assertAlmostEqual(h["age"], 5, delta=2)

    def test_old_heartbeat_without_phase_still_works(self):
        """兼容老格式：早期心跳只有 ts，没有 phase。

        不能因为缺字段就报错——升级瞬间那个文件还是旧的。
        """
        now = int(time.time())
        with _fake_hb({"ts": now - 3}):
            h = _heartbeat()
        self.assertEqual(h["phase"], "")
        self.assertAlmostEqual(h["age"], 3, delta=2)

    def test_future_ts_clamped_to_zero(self):
        """时钟回拨时 age 不能是负数（会让 alive 判定错乱）。"""
        with _fake_hb({"ts": int(time.time()) + 3600}):
            h = _heartbeat()
        self.assertEqual(h["age"], 0)


class TestAliveThreshold(unittest.TestCase):
    """`alive` 的边界：阈值内算活，超过算断。"""

    def test_within_threshold_is_alive(self):
        now = int(time.time())
        with _fake_hb({"ts": now - (HEARTBEAT_MAX_AGE - 5), "phase": "polling"}):
            h = _heartbeat()
        self.assertLess(h["age"], HEARTBEAT_MAX_AGE)

    def test_beyond_threshold_is_dead(self):
        now = int(time.time())
        with _fake_hb({"ts": now - (HEARTBEAT_MAX_AGE + 5), "phase": "polling"}):
            h = _heartbeat()
        self.assertGreaterEqual(h["age"], HEARTBEAT_MAX_AGE)


class TestFailScenarioDistinguishable(unittest.TestCase):
    """这是本文件的核心：三种「不更新」必须能被区分。"""

    def test_retry_phase_carries_error(self):
        """接口报错退避中：能看出「还活着、在重试、报什么错」。"""
        now = int(time.time())
        payload = {"ts": now, "phase": "retry", "fails": 3,
                   "err": "getupdates 500"}
        with _fake_hb(payload):
            h = _heartbeat()
        self.assertEqual(h["phase"], "retry")
        self.assertEqual(h["fails"], 3)
        self.assertIn("500", h["err"])

    def test_polling_phase_means_waiting_not_stuck(self):
        """polling 是正常等待态：长轮询挂起几十秒不该被当成故障。"""
        with _fake_hb({"ts": int(time.time()), "phase": "polling", "fails": 0}):
            h = _heartbeat()
        self.assertEqual(h["phase"], "polling")
        self.assertEqual(h["fails"], 0)

    def test_dead_process_has_stale_ts(self):
        """进程真死了：即便最后 phase 是 polling，age 也会持续变大到超阈值。

        这正是老实现的问题——它区分不出「正常挂起」和「挂起后死了」，
        现在靠 age 超过阈值来判定，UI 上分「正常」和「已断开」。
        """
        old = int(time.time()) - 60 * 60 * 15      # 15.6 小时的实发值
        with _fake_hb({"ts": old, "phase": "polling"}):
            h = _heartbeat()
        self.assertGreaterEqual(h["age"], HEARTBEAT_MAX_AGE)
        self.assertEqual(h["phase"], "polling")     # 状态还是旧的，但 age 已判死


if __name__ == "__main__":
    unittest.main()
