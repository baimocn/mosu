# -*- coding: utf-8 -*-
"""把墨溯挂到 Pi（agent harness）上，让 Pi 的 agent 能直接查微信数据。

背景（2026-10-31）：
    白墨给了 `D:\\Desktop\\llm自动化\\pi-main`——一个 agent harness（Pi）。
    Pi 支持 MCP stdio，墨溯本来就有 MCP Server，两边一接就通：
    **Pi 的 agent 有了「手」，能实时查墨溯解出来的微信数据并自主分析。**

实测结果：
    - Pi agent 自主调 mosu_status / mosu_stats / mosu_contacts /
      mosu_profile 做了多步分析，输出带具体数字与依据
    - MCP server 冷启动 ~7s（解密 + 建索引），之后单次工具调用 0.0–1.0s

顺带修掉的三个坑（不修就接不通）：
    1. Pi 默认用 `role: "developer"` 发系统提示 → 8787 网关的上游渠道校验
       会拒（"Illegal API invocation from an unapproved channel"）。
       解法：provider 加 `compat.supportsDeveloperRole: false`。
    2. Pi 的 provider 默认 `api: "openai-responses"`，但本机网关只有
       `/v1/chat/completions`（`/v1/responses` 404）。
       解法：改成 `openai-completions`。
    3. Pi 默认**不**把 apiKey 作为 `Authorization: Bearer` 发送
       （`authHeader` 默认 false）→ 网关认不出渠道。
       解法：provider 加 `authHeader: true`。

用法：
    python scripts/install_pi_mcp.py            # 装/更新
    python scripts/install_pi_mcp.py --check    # 只体检不改动
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable or "python"

SERVER_NAME = "mosu"


# ----------------------------------------------------------------- 配置片段

def mcp_entry() -> dict:
    """墨溯作为 MCP server 的配置（Pi / Claude Code / Cursor 通吃）。"""
    return {
        "command": PY,
        "args": [str(ROOT / "core" / "mcp_server.py")],
        "cwd": str(ROOT),
        "env": {"PYTHONIOENCODING": "utf-8"},
        # direct = 工具直接出现在模型面前（默认 codemode 会让模型看不见列表）
        "exposure": "direct",
        "description": "墨溯：本地微信聊天记录只读查询。列会话、读消息、搜关键词、"
                       "看统计、读朋友圈、生成报告、导出记录。分析自己的微信数据时用这个。",
    }


def models_fix() -> dict:
    """Pi 的 provider 需要的三处修正（网关不认 developer 角色 / responses 端点）。"""
    return {"api": "openai-completions", "authHeader": True,
            "compat": {"supportsDeveloperRole": False}}


# ----------------------------------------------------------------- 读写

def _read_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"  [!] {p} 读不了（{e}），当空文件处理")
        return {}


def _write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def install_mcp() -> Path:
    """写 ~/.pi/agent/mcp.json（用户级，所有项目都能用）。"""
    p = Path.home() / ".pi" / "agent" / "mcp.json"
    cfg = _read_json(p)
    servers = cfg.setdefault("mcpServers", {})
    old = servers.get(SERVER_NAME)
    servers[SERVER_NAME] = mcp_entry()
    _write_json(p, cfg)
    if old:
        print(f"  更新已存在的 {SERVER_NAME} 条目（原 command: "
              f"{(old.get('command') or '?')[:40]}）")
    else:
        print(f"  新增 {SERVER_NAME} 条目")
    return p


def install_models() -> Path | None:
    """修 ~/.pi/agent/models.json 里的 provider 配置。"""
    p = Path.home() / ".pi" / "agent" / "models.json"
    if not p.exists():
        print("  [跳过] models.json 不存在（Pi 还没配过 provider）")
        return None
    cfg = _read_json(p)
    changed = []
    for name, prov in (cfg.get("providers") or {}).items():
        if not isinstance(prov, dict):
            continue
        base = str(prov.get("baseUrl") or "")
        # 只修指向本机 WorkBuddy 网关的 provider
        if "127.0.0.1:8787" not in base and "localhost:8787" not in base:
            continue
        for k, v in models_fix().items():
            if prov.get(k) != v:
                prov[k] = v
                changed.append(f"{name}.{k}")
    if not changed:
        print("  provider 配置已正确，无需改动")
        return p
    _write_json(p, cfg)
    print(f"  修正字段：{', '.join(changed)}")
    return p


def check() -> int:
    """体检：MCP 连通性 + provider 配置。"""
    ok = True
    print("== Pi 是否可用 ==")
    if shutil.which("pi"):
        print("  [✓] pi 在 PATH")
    else:
        print("  [!] 找不到 pi —— 先 npm i -g @earendil-works/pi-coding-agent")
        ok = False

    print("\n== MCP 配置 ==")
    p = Path.home() / ".pi" / "agent" / "mcp.json"
    cfg = _read_json(p)
    entry = (cfg.get("mcpServers") or {}).get(SERVER_NAME)
    if not entry:
        print(f"  [!] {p} 里没有 {SERVER_NAME} 条目")
        ok = False
    else:
        cmd = Path(entry.get("command") or "")
        args = entry.get("args") or []
        target = Path(args[0]) if args else None
        if not cmd.exists():
            print(f"  [!] command 不存在：{cmd}")
            ok = False
        elif target and not target.exists():
            print(f"  [!] args[0] 不存在：{target}")
            ok = False
        else:
            print(f"  [✓] command {cmd.name} / {target.name if target else '?'}")
            print(f"      exposure = {entry.get('exposure', 'codemode（默认）')}")

    print("\n== provider 配置 ==")
    mp = Path.home() / ".pi" / "agent" / "models.json"
    mcfg = _read_json(mp)
    for name, prov in (mcfg.get("providers") or {}).items():
        if not isinstance(prov, dict):
            continue
        base = str(prov.get("baseUrl") or "")
        if "8787" not in base:
            continue
        print(f"  provider {name} @ {base}")
        for k, want in models_fix().items():
            got = prov.get(k)
            mark = "✓" if got == want else "✗"
            if got != want:
                ok = False
            print(f"    {mark} {k} = {json.dumps(got, ensure_ascii=False)}"
                  f"（应为 {json.dumps(want, ensure_ascii=False)}）")

    print("\n== 结论 ==")
    print("  可以用了" if ok else "  还没好，跑 `python scripts/install_pi_mcp.py` 修")
    return 0 if ok else 1


# ----------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="把墨溯装到 Pi harness 上")
    ap.add_argument("--check", action="store_true", help="只体检不改动")
    args = ap.parse_args()

    if args.check:
        return check()

    print("== 安装 ==")
    p = install_mcp()
    print(f"  MCP 配置 → {p}")
    p2 = install_models()
    if p2:
        print(f"  provider 配置 → {p2}")
    print("\n下一步：")
    print("  1. 验证连接：pi mcp list")
    print("  2. 提问试试：cd 到墨溯目录，跑 pi 然后问「用 mosu 查我的聊天统计」")
    print("  3. 检查：python scripts/install_pi_mcp.py --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
