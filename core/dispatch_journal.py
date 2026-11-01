# -*- coding: utf-8 -*-
"""派活通道的运行时事件日志。

对标 Lancer 的 `apps/desktop/electron/control/runtime-journal.ts`。

为什么需要这个
--------------
迭代 9 之前，墨溯的派活通道只有 `send/queue/steer/status/runs/wait`。
外部程序派了活，只能 `wait` 干等一个结果——**中间发生了什么它完全看不见**：
不知道模型正在查哪个工具、查到第几步、是不是卡住了。

Lancer 的 Control API 有 `stream` 操作，返回 `ControlStream`，
服务端把它转成 **NDJSON 逐行推**（`Content-Type: application/x-ndjson`），
每行一个事件，带**全局单调 seq**。客户端拿 `since` 游标断线重连，
服务端先回放 `seq > since` 的历史，再接上实时流。

这就是本模块要抄的东西：一份带 seq 的事件日志，
让 `stream` 既有「补历史」又有「接实时」。

抄的三样语义
------------
1. **全局单调 seq**（不是每会话独立）。
   `logs(since)` 回放 `seq > since` 的事件。
   为什么全局而不是每会话：seq 只是个游标，单调就够了。
   分会话编号会让人以为 seq 在会话之间可比，然后写出错误的过滤条件。

2. **订阅先于回放**。
   Lancer 的注释写得很明确：
   "A subscription is installed before replay so a cursor never races live publication."
   顺序反了 → 回放和实时之间漏事件。

3. **凭证脱敏**（`scrub`）。
   事件里有工具名、参数、输出——**API key 可能就在里面**
   （比如 `test_connection` 的调用参数、`search` 命中了聊天记录里的密码）。
   事件日志是要推给外部程序看的，不脱敏等于把密钥广播出去。
   Lancer 的规则：先从 key/token/secret/password 命中的字段**采集**凭证，
   再把凭证的字面量从所有文本里替换成 `[REDACTED]`。

本模块额外加的两条
----------------
- **有界保留**：Lancer 的 journal 是纯内存不截断，墨溯加了上限
  （`MAX_EVENTS`）。理由：墨溯的派活服务是**长驻**的（用户开着 GUI 一整天），
  不截断的话内存会一直涨。
- **纯标准库**：不引第三方包（守零依赖铁律）。

事件形状
--------
    {"seq": 12, "ts": 1762150800.0, "kind": "toolStarted",
     "runId": "20261103-...", "text": "tool_numbers {...}", ...}

`kind` 取值：runStarted / toolStarted / toolFinished / assistantDelta /
status / steer / runCompleted / runFailed / runCancelled。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

# 事件保留上限。超出后丢最旧的。
# Lancer 不截断（它是桌面端短生命周期进程），墨溯是长驻服务，必须截。
MAX_EVENTS = 500

# 事件类型白名单。用白名单而不是「任意 kind 都收」——
# 免得内部代码拼错 kind 之后静默传出去，下游按 kind 分支的逻辑全落空。
KINDS = (
    "runStarted",
    "toolStarted",
    "toolFinished",
    "assistantDelta",
    "status",
    "steer",
    "runCompleted",
    "runFailed",
    "runCancelled",
)

# 敏感字段名（用来决定「这个字符串是不是凭证」）。
# 与 Lancer 的 sensitiveKey 同口径：/key|token|secret|password|authorization/i
_SENSITIVE_KEY = re.compile(r"key|token|secret|password|authorization", re.I)

# 显式的凭证字面量模式。
# 这些不用出现在 key 名里也一定是凭证：
#   - sk-xxx / gh[pousr]_xxx / AKIAxxx（常见服务商的 API key 前缀）
#   - Bearer <token>（HTTP 头）
#   - key=xxx / token: xxx / "password": "xxx"（赋值形式）
_LITERAL_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{12,}\b"),
)

# 赋值形式：`key = value` / `key: value` / `"key":"value"`
_ASSIGN_PATTERN = re.compile(
    r"((?:[\w-]*(?:key|token|secret|password)[\w-]*)\s*[\"']?\s*(?:=|:|=>)\s*)"
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}&]+)",
    re.I,
)

REDACTED = "[REDACTED]"


def scrub(text: str, secrets: set[str]) -> str:
    """把文本里的凭证替换成 [REDACTED]。

    `secrets` 是**已采集**的凭证集合——采集与替换必须分两步：
    先把结构化字段（key 名命中的）里的凭证收进集合，
    再把集合里的字面量从所有文本里抹掉。
    只做第二步（不采集）会漏掉「字段名叫 notes、值其实是 key」的情况。
    """
    if not text:
        return text

    def _sub_assign(m: re.Match) -> str:
        prefix, value = m.group(1), m.group(2)
        raw = value
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        if raw and raw != REDACTED:
            secrets.add(raw)
        quote = ""
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote = value[0]
        return f"{prefix}{quote}{REDACTED}{quote}"

    def _sub_literal(m: re.Match) -> str:
        whole = m.group(0)
        # Bearer xxx → 保留 "Bearer " 前缀
        if whole.lower().startswith("bearer "):
            token = whole[7:].strip()
            if token:
                secrets.add(token)
            return "Bearer " + REDACTED
        if whole and whole != REDACTED:
            secrets.add(whole)
        return REDACTED

    text = _ASSIGN_PATTERN.sub(_sub_assign, text)
    for rx in _LITERAL_PATTERNS:
        text = rx.sub(_sub_literal, text)
    # 把已采集的凭证字面量逐个替换掉（长的先替，避免短的是长的子串）
    for secret in sorted(secrets, key=len, reverse=True):
        if secret and secret != REDACTED:
            text = text.replace(secret, REDACTED)
    return text


def collect_secrets(value: Any, secrets: set[str], sensitive: bool = False,
                    seen: set[int] | None = None) -> None:
    """递归采集值里的凭证字面量。

    `sensitive=True` 时（父级字段名命中了敏感词）整个子树都算敏感。
    与 Lancer 的 capture() 同口径。
    """
    if seen is None:
        seen = set()
    if isinstance(value, str):
        if sensitive and value:
            secrets.add(value)
        return
    if not isinstance(value, dict):
        return
    if id(value) in seen:
        return
    seen.add(id(value))
    for name, item in value.items():
        collect_secrets(item, secrets,
                        sensitive or bool(_SENSITIVE_KEY.search(str(name))),
                        seen)


def redact_value(value: Any, secrets: set[str],
                 seen: set[int] | None = None) -> Any:
    """把结构化值里的敏感字段与凭证字面量替换掉。

    与 Lancer 的 redact() 同口径：敏感**字段名**整个值替换成 [REDACTED]，
    其余字符串走 scrub。
    """
    if isinstance(value, str):
        return scrub(value, secrets)
    if not isinstance(value, (dict, list)):
        return value
    if seen is None:
        seen = set()
    if id(value) in seen:
        return "[Circular]"
    seen.add(id(value))
    if isinstance(value, list):
        out = [redact_value(v, secrets, seen) for v in value]
    else:
        out = {}
        for name, item in value.items():
            if _SENSITIVE_KEY.search(str(name)):
                out[name] = REDACTED
            else:
                out[name] = redact_value(item, secrets, seen)
    seen.discard(id(value))
    return out


class EventJournal:
    """带 seq 的运行时事件日志。

    线程安全：多个线程（派活的 worker、GUI 的任务线程）都可能往里写。
    """

    def __init__(self, max_events: int = MAX_EVENTS):
        self.max_events = max(1, int(max_events))
        self._lock = threading.RLock()
        self._seq = 0
        self._events: list[dict] = []
        self._secrets: set[str] = set()
        self._listeners: set[Callable[[dict], None]] = set()

    # ------------------------------------------------------------------ 写
    def append(self, kind: str, text: str = "", run_id: str = "",
               secrets: set[str] | None = None,
               **fields: Any) -> dict:
        """追加一条事件并广播。

        `secrets` 传进来的是**这批数据里采集到的凭证**——
        调用方（agent 引擎）已经知道哪些字段敏感，这里只负责把它们记下来，
        后续所有文本都会被抹掉。
        """
        if kind not in KINDS:
            # 拼错的 kind 直接归成 status，而不是静默塞进去——
            # 下游按 kind 分支时，静默的未知 kind 会让所有分支都不命中。
            kind = "status"
        with self._lock:
            self._secrets |= (secrets or set())
            clean = scrub(text or "", self._secrets)
            for name, item in list(fields.items()):
                if isinstance(item, str):
                    fields[name] = scrub(item, self._secrets)
            self._seq += 1
            ev = {
                "seq": self._seq,
                "ts": time.time(),
                "kind": kind,
                "runId": run_id or "",
                "text": clean,
                **fields,
            }
            self._events.append(ev)
            if len(self._events) > self.max_events:
                # 丢最旧的。保留顺序 = 时间顺序（追加式）。
                del self._events[:len(self._events) - self.max_events]
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(dict(ev))
            except Exception:
                # 订阅者自己的异常不能影响写日志的主流程
                pass
        return dict(ev)

    # ------------------------------------------------------------------ 读
    def logs(self, since: int = 0, run_id: str = "") -> list[dict]:
        """回放事件。`since` 是**全局 seq 游标**（取 seq > since）。

        `run_id` 非空时只回放那一轮的事件。
        """
        with self._lock:
            out = [dict(e) for e in self._events
                   if e["seq"] > since and (not run_id or e.get("runId") == run_id)]
        out.sort(key=lambda e: e["seq"])
        return out

    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def subscribe(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        """订阅实时事件。返回取消订阅的函数。"""
        with self._lock:
            self._listeners.add(listener)
        def _unsub():
            with self._lock:
                self._listeners.discard(listener)
        return _unsub

    def state(self, run_id: str = "") -> dict:
        """当前状态摘要（给 status 用）。"""
        with self._lock:
            evs = [e for e in self._events if not run_id or e.get("runId") == run_id]
            latest_status = ""
            for e in evs:
                if e["kind"] == "status":
                    latest_status = e.get("text") or ""
            return {
                "events": len(evs),
                "lastSeq": self._seq,
                "status": latest_status,
                "secretsRedacted": len(self._secrets),
            }

    def alias_run(self, old_id: str, new_id: str) -> None:
        """把某一轮的事件改挂到另一个 id 下。

        落盘时 `save_run` 遇到同名会加后缀（不覆盖旧数据），
        返回的 id 可能 ≠ 传入的 id。外部程序拿到的是**返回值**，
        所以事件里的 runId 也要跟着改，否则
        `stream --run-id <用户看到的 id>` 过滤不到任何事件。

        改写而不是追加：事件是「这一轮发生了什么」的记录，
        runId 是**归属标记**不是内容。归属变了就该改，
        留两条不同 runId 的同一轮事件只会让调用方困惑。
        """
        if not old_id or not new_id or old_id == new_id:
            return
        with self._lock:
            changed: list[dict] = []
            for e in self._events:
                if e.get("runId") == old_id:
                    e["runId"] = new_id
                    changed.append(dict(e))
        # 通知订阅者：改写后重发一次（下游按 seq 去重即可）
        with self._lock:
            listeners = list(self._listeners)
        for ev in changed:
            for fn in listeners:
                try:
                    fn(dict(ev))
                except Exception:
                    pass

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._secrets.clear()
