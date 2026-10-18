# -*- coding: utf-8 -*-
"""
墨溯 core —— 输出格式器

输出格式：json | jsonl | table | csv | markdown
"""
from __future__ import annotations

import csv as _csv
import io
import json
import sys
import unicodedata
from datetime import datetime


def _width(s: str) -> int:
    """按终端显示宽度计算（中文/全角算 2）。"""
    w = 0
    for ch in str(s):
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _pad(s: str, w: int) -> str:
    s = str(s)
    return s + " " * max(0, w - _width(s))


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    return s.replace("\n", " ⏎ ").replace("\r", " ")


def _rows_of(data) -> list[dict]:
    if isinstance(data, list):
        return [d if isinstance(d, dict) else {"value": d} for d in data]
    if isinstance(data, dict):
        for k in ("messages", "sessions", "contacts", "entries", "keywords", "items"):
            if isinstance(data.get(k), list):
                return [d if isinstance(d, dict) else {"value": d} for d in data[k]]
        return [data]
    return [{"value": data}]


def render_table(data, columns: list[str] | None = None, max_width: int = 46) -> str:
    rows = _rows_of(data)
    if not rows:
        return "（无数据）"
    if columns:
        cols = columns
    else:
        cols = []
        for r in rows[:40]:
            for k in r.keys():
                if k not in cols:
                    cols.append(k)
    # 裁剪列数，避免超宽终端
    if len(cols) > 10:
        cols = cols[:10]

    def trunc(v):
        s = _cell(v)
        if _width(s) > max_width:
            s = s[:max_width - 1] + "…"
        return s

    widths = {}
    for c in cols:
        w = _width(c)
        for r in rows:
            w = max(w, _width(trunc(r.get(c))))
        widths[c] = min(w, max_width + 2)

    out = ["  " + "  ".join(_pad(c, widths[c]) for c in cols)]
    out.append("  " + "  ".join("-" * widths[c] for c in cols))
    for r in rows:
        out.append("  " + "  ".join(_pad(trunc(r.get(c)), widths[c]) for c in cols))
    return "\n".join(out)


def render_csv(data, columns: list[str] | None = None) -> str:
    rows = _rows_of(data)
    if not rows:
        return ""
    if not columns:
        columns = []
        for r in rows:
            for k in r.keys():
                if k not in columns:
                    columns.append(k)
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in columns])
    return buf.getvalue().rstrip("\r\n")


def render_markdown(data, columns: list[str] | None = None) -> str:
    rows = _rows_of(data)
    if not rows:
        return "（无数据）"
    if not columns:
        columns = []
        for r in rows:
            for k in r.keys():
                if k not in columns:
                    columns.append(k)
    head = "| " + " | ".join(str(c) for c in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(_cell(r.get(c)).replace("|", "\\|") for c in columns) + " |"
            for r in rows]
    return "\n".join([head, sep] + body)


def render_json(data, meta=None) -> str:
    payload = data
    if meta:
        payload = {"data": data, "meta": meta}
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def render_jsonl(data) -> str:
    rows = _rows_of(data)
    return "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)


def fmt_time(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def emit(data, fmt: str = "table", meta=None, columns: list[str] | None = None,
         stream=None, raw=None) -> None:
    """raw: 机器消费用的原始数据（json/jsonl 优先输出它，保留英文字段名）"""
    stream = stream or sys.stdout      # 默认参数不能写 sys.stdout：导入时绑定，
                                       # redirect_stdout / 测试就捕不到输出
    fmt = (fmt or "table").lower()
    if fmt in ("json", "jsonl") and raw is not None:
        data = raw
    if fmt == "json":
        text = render_json(data, meta)
    elif fmt == "jsonl":
        text = render_jsonl(data)
    elif fmt == "csv":
        text = render_csv(data, columns)
    elif fmt == "markdown":
        text = render_markdown(data, columns)
    else:
        text = render_table(data, columns)
        if meta and fmt == "table":
            tail = "  ".join(f"{k}={v}" for k, v in meta.items() if v is not None)
            if tail:
                text += f"\n\n  ({tail})"
    print(text, file=stream)
