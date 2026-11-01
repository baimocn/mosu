# -*- coding: utf-8 -*-
"""全局数据锁 —— SQLite 连接跨线程复用的唯一守门人。

背景（2026-11-02）：
    墨溯的 `Dataset.conn()` 用 `check_same_thread=False` 允许跨线程，
    但 SQLite 的**同一个连接对象不能被多线程同时使用**——
    底层游标会互相踩，表现为 `database disk image is malformed`
    或结果错乱（比崩溃更可怕，因为它是静默的）。

    以前 GUI 侧有个局部 `_ds_lock` 保护数据 API，
    但 Agentic 洞察的子代理要并行跑，锁必须下沉到 core 层，
    否则「GUI 查询」和「子代理查询」会各拿各的锁，等于没锁。

为什么要 RLock 而不是 Lock：
    GUI 的数据 API 持锁期间，内部又会回调到需要取锁的地方
    （比如 api 函数内部再调 get_ds / 其它 api_*），
    普通 Lock 会自己把自己锁死（自死锁）。

用法：
    from core.locks import DS_LOCK
    with DS_LOCK:
        ...  # 任何碰 Dataset / SQLite 的地方
"""
from __future__ import annotations

import threading

# 全局唯一的 SQLite 访问锁。RLock = 可重入。
DS_LOCK = threading.RLock()
