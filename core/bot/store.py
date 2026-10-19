# -*- coding: utf-8 -*-
"""对话上下文持久化。

原来的上下文只在 `BotService.history` 这个内存 dict 里：进程一重启就全丢，
微信通道又是常驻的（几天不关），所以「AI 忘了刚才说的话」是必然会发生的。

这一层把上下文落盘到 `~/.mosu/conversations/`，规则：

1. **每个会话一个 JSON 文件**，文件名是会话 id 的安全化 + 哈希后缀（wxid 含 @ 和
   中文，直接当文件名在 Windows 上会有坑）。
2. **写时截断**：每个会话只保留最近 `limit` 轮，超出即丢最旧的，文件不会无界增长。
3. **写时去敏**：默认只落「最近 N 轮」，且**不落回复全文**以外的东西——这里其实
   没有额外数据要脱敏，落盘的就是对话本身，仍然提醒用户这是明文。
4. **失败不致命**：磁盘满、权限不足、文件损坏，一律静默降级为纯内存，绝不让
   聊天因为存档失败而出错。
5. **可关闭**：`MOSU_BOT_HISTORY=0` 或显式传 `store=None` 即退回内存模式。

隐私提示：这是你和你自己机器上的 AI 的对话记录，明文存在 `~/.mosu/conversations/`。
要清空直接删这个目录。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

_SAFE = re.compile(r"[^0-9A-Za-z_-]")


def home() -> Path:
    return Path(os.environ.get("MOSU_HOME") or (Path.home() / ".mosu"))


def dir_for() -> Path:
    return home() / "conversations"


def _fname(conv_id: str) -> str:
    """会话 id → 安全的文件名。前 32 个安全字符 + 8 位哈希（防撞名与超长）。"""
    cid = (conv_id or "default").strip()
    safe = _SAFE.sub("_", cid)[:32] or "default"
    tag = hashlib.md5(cid.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{tag}.json"


def enabled() -> bool:
    return os.environ.get("MOSU_BOT_HISTORY", "1") != "0"


class ConversationStore:
    """对话上下文的读写。线程安全（每会话一把锁），失败一律降级。"""

    def __init__(self, limit: int = 20, ttl_days: int = 30):
        self.limit = limit                 # 每个会话保留的最大轮数（1 轮 = 用户+助手各一条）
        self.ttl = ttl_days * 86400
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._failed = False               # 一旦落盘失败就不再重试，避免每轮都吃异常

    def _lock(self, cid: str) -> threading.Lock:
        with self._guard:
            lk = self._locks.get(cid)
            if lk is None:
                lk = self._locks[cid] = threading.Lock()
            return lk

    # ---------------------------------------------------------------- 读
    def load(self, conv_id: str | None) -> list[dict]:
        if not enabled() or self._failed:
            return []
        cid = conv_id or "default"
        f = dir_for() / _fname(cid)
        try:
            if not f.exists():
                return []
            if time.time() - f.stat().st_mtime > self.ttl:
                return []                    # 过期视为空，不去删（下次写会覆盖）
            data = json.loads(f.read_text(encoding="utf-8"))
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return []
            return [x for x in items
                    if isinstance(x, dict) and x.get("role") and x.get("content")][-self.limit * 2:]
        except Exception:
            self._failed = True             # 磁盘坏了就永久走内存，不刷错误
            return []

    # ---------------------------------------------------------------- 写
    def append(self, conv_id: str | None, turns: list[dict]) -> None:
        """把新一轮（[user, assistant]）追加到文件里，内部按 limit 截断。"""
        if not enabled() or self._failed or not turns:
            return
        cid = conv_id or "default"
        with self._lock(cid):
            try:
                d = dir_for()
                d.mkdir(parents=True, exist_ok=True)
                f = d / _fname(cid)
                items = self.load(conv_id)
                items.extend(turns)
                if len(items) > self.limit * 2:
                    items = items[-self.limit * 2:]
                f.write_text(json.dumps(
                    {"convId": cid, "updatedAt": int(time.time()), "items": items},
                    ensure_ascii=False), encoding="utf-8")
            except Exception:
                self._failed = True

    def clear(self, conv_id: str | None = None) -> int:
        """清空一个或全部会话，返回删除的文件数。"""
        d = dir_for()
        if not d.exists():
            return 0
        targets = [d / _fname(conv_id)] if conv_id else list(d.glob("*.json"))
        n = 0
        for f in targets:
            try:
                f.unlink()
                n += 1
            except Exception:
                pass
        return n

    def stats(self) -> dict:
        d = dir_for()
        if not d.exists():
            return {"dir": str(d), "files": 0, "bytes": 0}
        files = list(d.glob("*.json"))
        return {"dir": str(d), "files": len(files),
                "bytes": sum(f.stat().st_size for f in files),
                "enabled": enabled() and not self._failed}