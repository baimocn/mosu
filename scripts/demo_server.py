# -*- coding: utf-8 -*-
"""以「演示模式」启动 GUI，用于生成公开截图。

为什么需要这个脚本：
  - 环境变量在不同启动方式下会丢（Git Bash 的 nohup、taskkill 后残留进程、
    cli 内部子进程等），手工 set 再启动极易变成"以为开了其实没开"，
    于是截出来的图里还是真实姓名和账号。
  - 这个脚本直接在自己进程里设好 os.environ 再调 run_gui，
    不经过任何 shell，不存在传递丢失。

用法:
    python scripts/demo_server.py            # 默认 8788
    python scripts/demo_server.py --port 8790
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 必须在 import gui.server / core.dataset 之前设置：
# 那些模块在函数调用时才读，但提前设好更稳。
os.environ["MOSU_DEMO"] = "1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--db", default=None, help="覆盖 MOSU_DB_PATH")
    a = ap.parse_args()

    if a.db:
        os.environ["MOSU_DB_PATH"] = a.db

    corpus = ROOT / "persona" / "corpus.jsonl"
    if not corpus.exists():
        print(f"[x] 语料不存在：{corpus}")
        return 2

    # 自检：确认脱敏真的生效，避免"以为开了其实没开"
    from core.dataset import Dataset
    try:
        ds = Dataset(quiet=True)
        if not ds.demo:
            print("[x] MOSU_DEMO 未生效，拒绝启动（否则截图会泄漏真实信息）")
            return 3
        names = [s["displayName"] for s in ds.sessions()[:3]]
        print(f"[mosu-demo] 脱敏已生效，样例会话名: {names}")
    except Exception as e:  # noqa: BLE001
        print(f"[!] 预检失败（继续启动）: {e}")

    from gui.server import run_gui
    print(f"[mosu-demo] 启动 http://127.0.0.1:{a.port} （演示模式，姓名/正文/账号已脱敏）")
    run_gui(corpus, port=a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
