# -*- coding: utf-8 -*-
"""桌面快捷方式 —— 「墨溯」一键入口。

普通用户不该被要求打开终端。双击桌面图标即可：服务没起就拉起并开浏览器，
已经跑着就直接开浏览器（端口占用判定在 gui/server.py 里，这里只负责造入口）。

Windows 用 PowerShell 的 WScript.Shell COM 建 .lnk（可绑定图标，原生体验）。
macOS/Linux 退化成可执行脚本，不改无谓的封装。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "gui" / "assets" / "mosu.ico"
APP_NAME = "墨溯"


def _ps(cmd: str, timeout: int = 90):
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout)


def _launcher() -> tuple[str, str]:
    """返回 (可执行程序, 参数)。优先 pythonw：后台启动，不闪黑框。"""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    if pyw.exists():
        return str(pyw), f'-u "{ROOT / "cli" / "mosu.py"}" gui'
    return str(exe), f'-u "{ROOT / "cli" / "mosu.py"}" gui'


# ---------------------------------------------------------------- Windows
def _deskdirs_windows() -> list[Path]:
    """用户可能把桌面重定向到别处（本机就搬到了 D:\\Desktop），
    所以「系统认定的桌面」和「真实在用的桌面」都放一份。"""
    dirs: list[Path] = []
    r = _ps("Write-Output ([Environment]::GetFolderPath('Desktop'))")
    sysd = (r.stdout or "").strip().splitlines()
    if sysd and sysd[0]:
        dirs.append(Path(sysd[0]))
    # OneDrive 重定向（微软默认会把桌面塞进 OneDrive）
    od = os.environ.get("OneDrive")
    if od:
        dirs.append(Path(od) / "Desktop")
    # 常见重定向位置（用户自述真实桌面在 D 盘）
    for cand in (Path("D:/Desktop"), Path("C:/Users/Public/Desktop")):
        if cand.is_dir():
            dirs.append(cand)
    out, seen = [], set()
    for d in dirs:
        k = str(d).lower()
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def _shortcut_windows() -> list[str]:
    target, arguments = _launcher()
    made: list[str] = []
    for d in _deskdirs_windows():
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            continue
        lnk = d / f"{APP_NAME}.lnk"
        ps = (
            "$s = New-Object -ComObject WScript.Shell;"
            f"$c = $s.CreateShortcut('{lnk}');"
            f"$c.TargetPath = '{target}';"
            f"$c.Arguments = '{arguments}';"
            f"$c.WorkingDirectory = '{ROOT}';"
            "$c.Description = '墨溯 · 微信数据本地控制台';"
        )
        if ICON.exists():
            ps += f"$c.IconLocation = '{ICON},0';"
        ps += "$c.Save(); Write-Output 'ok'"
        try:
            r = _ps(ps)
        except Exception as e:
            print(f"[x] 建快捷方式失败（{d}）：{type(e).__name__}: {e}")
            continue
        if r.returncode == 0 and lnk.exists():
            made.append(str(lnk))
        else:
            detail = ((r.stderr or "") + (r.stdout or "")).strip()[:200]
            print(f"[x] 建快捷方式失败（{d}）：{detail}")
    return made


# ---------------------------------------------------------------- 其他平台
def _shortcut_posix() -> list[str]:
    exe, arguments = _launcher()
    made: list[str] = []
    d = Path.home() / "Desktop"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        print(f"[x] 找不到桌面目录：{d}")
        return made
    if sys.platform == "darwin":
        f = d / f"{APP_NAME}.command"
        f.write_text(f'#!/bin/sh\nexec "{exe}" {arguments}\n', encoding="utf-8")
        f.chmod(0o755)
        made.append(str(f))
    else:
        f = d / f"{APP_NAME}.desktop"
        f.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            "Comment=墨溯 · 微信数据本地控制台\n"
            f"Exec={exe} {arguments}\n"
            f"Path={ROOT}\n"
            "Terminal=false\n"
            "Categories=Utility;\n", encoding="utf-8")
        f.chmod(0o755)
        made.append(str(f))
    return made


def create_shortcut() -> int:
    if os.name != "nt" and sys.platform != "darwin" and not sys.platform.startswith("linux"):
        print("[x] 未知平台，跳过快捷方式")
        return 1
    made = _shortcut_windows() if os.name == "nt" else _shortcut_posix()
    if not made:
        print("[x] 未能在任何桌面目录创建快捷方式")
        return 1
    for m in made:
        print(f"✅ 桌面快捷方式：{m}")
    print("   双击即可打开墨溯（服务未启动时会自动拉起）")
    return 0


def remove_shortcut() -> int:
    if os.name != "nt":
        print("[i] 非 Windows 请手动删除桌面上的墨溯入口")
        return 0
    removed = 0
    for d in _deskdirs_windows():
        for lnk in (d / f"{APP_NAME}.lnk",):
            if lnk.exists():
                try:
                    lnk.unlink()
                    print(f"✅ 已删除 {lnk}")
                    removed += 1
                except Exception as e:
                    print(f"[x] 删除失败 {lnk}：{e}")
    if not removed:
        print("[i] 桌面上没找到墨溯快捷方式")
    return 0
