# -*- coding: utf-8 -*-
"""墨溯 core —— 增量导出状态管理

导出一遍很慢（23 万消息要解压、图片要解密落盘）。增量导出让第二次之后只处理
新消息：

  - HTML：重渲染全部消息，但媒体文件命中磁盘缓存直接跳过（大头）
  - TXT / CSV：新消息追加到文件尾
  - JSON：读旧文件 → 合并去重 → 重写

状态文件 `<输出目录>/.mosu-state.json`：

    {"sessionId": "...", "lastExportTime": 1760000000,
     "count": 1463, "exportedAt": "2026-10-18 11:20:00"}
"""
from __future__ import annotations

import json
from pathlib import Path

STATE_NAME = ".mosu-state.json"


def state_path(out_dir: str | Path) -> Path:
    return Path(out_dir) / STATE_NAME


def load_state(out_dir: str | Path) -> dict | None:
    p = state_path(out_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_state(out_dir: str | Path, session_id: str, last_time: int, count: int) -> dict:
    import time as _t
    st = {
        "sessionId": session_id,
        "lastExportTime": int(last_time or 0),
        "count": int(count or 0),
        "exportedAt": _t.strftime("%Y-%m-%d %H:%M:%S"),
    }
    p = state_path(out_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return st


# ---------------------------------------------------------------- 消息合并
def _msg_key(m: dict):
    return (m.get("createTime") or 0, m.get("localId") or 0)


def merge_messages(old: list[dict], new: list[dict]) -> tuple[list[dict], int]:
    """合并两次导出的消息，按 (createTime, localId) 去重，时间正序返回。

    返回 (合并后的列表, 新增条数)。
    """
    seen = {_msg_key(m) for m in old}
    merged = list(old)
    added = 0
    for m in new:
        k = _msg_key(m)
        if k in seen:
            continue
        seen.add(k)
        merged.append(m)
        added += 1
    merged.sort(key=_msg_key)
    return merged, added


# ---------------------------------------------------------------- 文本追加
def append_text(path: str | Path, lines: list[str]) -> int:
    """TXT 追加（写文件不存在则创建）。"""
    if not lines:
        return 0
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln.rstrip("\n") + "\n")
    return len(lines)


def append_csv(path: str | Path, rows: list[list]) -> int:
    """CSV 追加；首行写表头（仅当文件不存在时）。"""
    import csv
    if not rows:
        return 0
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_file = not p.exists() or p.stat().st_size == 0
    with open(p, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["时间", "方向", "发送者", "类型", "内容"])
        for r in rows:
            w.writerow(r)
    return len(rows)


def merge_json_file(path: str | Path, new: dict) -> tuple[dict, int]:
    """读旧 JSON 导出 → 与新的合并 → 重写。返回 (合并结果, 新增条数)。"""
    p = Path(path)
    old: dict = {}
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    old_msgs = old.get("messages") or []
    new_msgs = new.get("messages") or []
    merged, added = merge_messages(old_msgs, new_msgs)
    out = dict(new)
    out["messages"] = merged
    if merged:
        out["session"] = {**new.get("session", {}), "messageCount": len(merged)}
    out["incremental"] = {"added": added, "previousCount": len(old_msgs)}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out, added
