# -*- coding: utf-8 -*-
"""分析结果的进程内缓存。

## 为什么需要

年度报告要分词（jieba）全库文本，实测 3.9s，其中分词占 72%。
用户每翻一次年、每切回来一次，都重跑一遍完全相同的计算——
**同一份数据、同一个参数，算出同一个结果，没有任何理由重算。**

## 为什么不用 lru_cache

`functools.lru_cache` 只按参数做键，不认数据变化。用户导入新的聊天记录后，
参数没变、结果应当变，lru_cache 会返回**过期数据**。这个工具的用户会
反复开关 GUI、反复重新解密，数据变化是常态。

所以键里必须带**数据签名**：数据变了，签名变，缓存自动失效。
签名要便宜（不能为了判断能否复用而先花掉一半的计算）。

## 为什么不做跨进程持久缓存

分词结果落盘要处理：数据变了怎么失效、磁盘写坏了怎么降级、
并发写同一文件怎么不打架。收益是"重启 GUI 后第一次打开更快"，
代价是一整套持久化可靠性问题。按奥卡姆剃刀，先只做进程内。
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict


class ResultCache:
    """带数据签名的 LRU 缓存（线程安全）。

    用法：
        cache = ResultCache(maxsize=32, ttl=3600)
        key = ("report", year, sig)
        hit = cache.get(key)
        if hit is None:
            val = compute()
            cache.set(key, val)
    """

    def __init__(self, maxsize: int = 32, ttl: float = 3600.0):
        self.maxsize = max(1, int(maxsize))
        self.ttl = float(ttl)
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key):
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return None
            val, ts = item
            if now - ts > self.ttl:
                # 过期就地删除，避免占着位置
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return val

    def set(self, key, value) -> None:
        with self._lock:
            self._data[key] = (value, time.time())
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {"size": len(self._data), "hits": self.hits, "misses": self.misses,
                    "hitRate": round(self.hits / total, 3) if total else 0.0,
                    "maxsize": self.maxsize, "ttl": self.ttl}


def dataset_signature(ds) -> str:
    """给当前数据集算一个便宜的指纹，用于缓存失效判断。

    只看「有哪些消息表、各多少条、最新一条的时间」——这三样足以判定
    "数据有没有变"，而代价是 163 次 `SELECT COUNT(*)`（走索引，毫秒级），
    远低于分词本身。

    刻意**不**读每条消息的内容去算哈希：那就等于把计算全做了一遍再判断
    要不要复用，缓存就失去意义了。
    """
    parts = []
    try:
        for sess in ds.sessions():
            parts.append(f"{sess.get('sessionId')}:{sess.get('messageCount')}:{sess.get('lastTime')}")
    except Exception:
        # 拿不到索引就返回一个每次不同的值，让缓存必然失效——
        # 宁可不命中，也不能命中错的数据
        return f"unknown-{time.time()}"
    raw = "|".join(sorted(parts))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# 全局实例：分析类结果（report / keyword / stats）共用。
# maxsize 取小值：每个条目都是几百 KB 的 dict，32 个已足够覆盖
# 「同一年翻来翻去 + 几个常用会话」的实际用法。
ANALYSIS_CACHE = ResultCache(maxsize=32, ttl=1800)
