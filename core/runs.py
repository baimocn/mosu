"""洞察运行记录：落盘 + 列举 + 回看。

为什么要有这个模块
------------------
迭代 2 之前，GUI 的洞察问答是「一次性」的：
  - 结果只活在 TaskHub 的内存字典里，GUI 一重启就全没了
  - 前端每 900ms 轮询一次 `/api/task?id=xxx`，拿的是**全量快照**再整体覆盖
  - 用户关掉页面，这次分析就白跑了

这三个问题在 Lancer 那边是靠「会话文件 + 事件流」解决的：每次运行都落盘成
一个可回看的记录，运行中通过单一广播通道推增量事件。对标过来就是本模块
加 TaskHub 的事件机制。

落盘位置：`~/.mosu/runs/`（MOSU_HOME 可覆盖），与密钥库同目录。
为什么不在项目目录下 `.cache/`：`.cache/` 是可随时清的解密缓存，
而运行记录是**用户资产**（他花了几十秒、几十次工具调用才得到的分析），
不能跟缓存一个命运。

格式：每轮一个 JSON 文件 `<时间戳>-<短哈希>.json`。
文件名带时间戳前缀 → `sorted()` 直接就是时间序，不需要读内容排序。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import time
from pathlib import Path

# 运行记录保留上限。超了就删最旧的。
# 为什么要上限：记录里含 evidence 原文和 steps，全量无限增长会把
# ~/.mosu 撑大。50 轮足够回看近期分析。
MAX_RUNS = 50

# 文件名里允许的字符（时间戳 + 短哈希），落盘前必须过一遍
_SAFE_RE = re.compile(r"[^0-9a-zA-Z_-]+")

# 进程内单调递增序号。用来在同一毫秒内区分先后——
# 只靠毫秒时间戳不够：50 次连续调用完全可能落在同一毫秒里，
# 那时所有 run_id 会一模一样（撞名=覆盖=丢数据），
# 而且列表按文件名排序就分不出先后了。
_SEQ = itertools.count()


def runs_dir() -> Path:
    """运行记录目录。MOSU_HOME 优先，与密钥库同源。"""
    home = os.environ.get("MOSU_HOME")
    base = Path(home) if home else (Path.home() / ".mosu")
    return base / "runs"


def _safe_stem(text: str, maxlen: int = 24) -> str:
    """把任意文本压成能进文件名的短标识。"""
    s = _SAFE_RE.sub("-", (text or "").strip())[:maxlen].strip("-")
    return s or "run"


def make_run_id(question: str) -> str:
    """生成运行 ID：毫秒时间戳 + 问题短哈希 + 进程内序号。

    三段各有分工：
      - 毫秒时间戳 → 文件名天然按时间序，`sorted()` 就是最新在前
      - 问题哈希   → 同一问题的记录在目录里能看出是同一类
      - 序号       → 同毫秒内的先后顺序 + 唯一性保证

    早先只有「秒级时间戳 + 哈希」，测试直接抓出两个坑：
      1. 同一秒内连问两次 → 文件名完全相同 → 后一次**静默覆盖**前一次
      2. 列表按文件名倒序 → 同秒内的顺序变成按哈希排，不是按真实先后

    秒改毫秒只解决了「一秒内多次」；再补序号才解决「同一毫秒内多次」。
    """
    ts = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    h = hashlib.sha256((question or "").encode("utf-8")).hexdigest()[:8]
    seq = next(_SEQ) % 100000
    return f"{ts}-{h}-{seq:05d}"


def _path_for(run_id: str) -> Path:
    """运行 ID → 文件路径。**必须防路径穿越**。

    run_id 会从 URL query 传进来（/api/runs/detail?id=...），
    不校验就是任意文件读。文件名只允许 `[0-9A-Za-z_-]`。
    """
    rid = (run_id or "").strip()
    if not rid or not re.fullmatch(r"[0-9A-Za-z_.-]+", rid) or ".." in rid:
        raise ValueError("非法的运行 ID")
    return runs_dir() / f"{rid}.json"


def save_run(record: dict) -> str:
    """把一次运行写盘，返回**实际使用的** run_id。

    写入是**原子**的：先写临时文件再 os.replace。
    理由：GUI 是多线程的，一个运行可能在另一个运行读列表时正好写一半，
    读到半个 JSON 会让整个历史面板报错。

    同名不覆盖：万一 run_id 撞了（毫秒时间戳 + 序号理论上仍可能撞），
    自动加后缀而不是覆盖——覆盖是数据丢失，宁可多留一份。
    所以返回的 id 可能**不等于**传进来的 id，调用方要用返回值。
    """
    rid = (record.get("id") or "").strip() or make_run_id(record.get("question", ""))
    record = dict(record)
    record["id"] = rid
    record.setdefault("savedAt", time.time())

    d = runs_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{rid}.json"
    if path.exists():
        base = rid
        for i in range(2, 100):
            cand = f"{base}-{i}"
            if not (d / f"{cand}.json").exists():
                rid = cand
                record["id"] = rid
                path = d / f"{rid}.json"
                break
    tmp = d / f".{rid}.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    _trim()
    return rid


def _trim(keep: int = MAX_RUNS) -> None:
    """超过上限就删最旧的。只删 .json，临时文件不动。"""
    d = runs_dir()
    if not d.exists():
        return
    files = sorted(d.glob("*.json"))
    if len(files) <= keep:
        return
    for p in files[:len(files) - keep]:
        try:
            p.unlink()
        except OSError:
            pass


def list_runs(limit: int = 30) -> list[dict]:
    """列出历史运行（摘要，不含全文 evidence/steps）。

    摘要 = 给列表页看的那几项：问题、时间、耗时、有没有分支。
    正文太大，全量返回会让历史面板一次拉几十 MB。
    """
    d = runs_dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True):
        if len(out) >= max(1, int(limit or 30)):
            break
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "id": data.get("id") or p.stem,
            "question": (data.get("question") or "")[:120],
            "answerHead": (data.get("answer") or "")[:160],
            "createdAt": data.get("createdAt") or data.get("savedAt"),
            "cost": data.get("cost"),
            "model": data.get("model"),
            # 计数字段一律带 Count 后缀：摘要里的 evidence 是整数，
            # 详情里的 evidence 是数组——同名不同型是最容易埋雷的写法，
            # 前端拿错字段时不会报错，只会渲染出空列表。
            "stepsCount": len(data.get("steps") or []),
            "evidenceCount": len(data.get("evidence") or []),
            "branchesCount": len(data.get("branches") or []),
        })
    return out


def load_run(run_id: str) -> dict | None:
    """读一条完整运行记录。找不到返回 None。"""
    try:
        path = _path_for(run_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def delete_run(run_id: str) -> bool:
    """删一条运行记录。返回是否真删了。"""
    try:
        path = _path_for(run_id)
    except ValueError:
        return False
    if not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False
