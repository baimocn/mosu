# -*- coding: utf-8 -*-
"""全局测试隔离。

最关键的一条：把 `MOSU_HOME` 指到临时目录。否则任何用到对话持久化、密钥库的
测试都会读写用户真实数据（`~/.mosu/conversations/`），既污染环境也会让断言依赖
机器上残留的历史。
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="mosu-test-")
os.environ["MOSU_HOME"] = _TMP
# 单测默认关掉语义缓存与常驻相关行为，保证确定性
os.environ.setdefault("MOSU_BOT_HISTORY", "1")