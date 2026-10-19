# -*- coding: utf-8 -*-
"""桌面快捷方式 —— 「墨溯」一键入口。

普通用户不该被要求打开终端。双击桌面图标即可：服务没起就拉起并开浏览器，
已经跑着就直接开浏览器（端口占用判定在 gui/server.py 里，这里只负责造入口）。

Windows 用 PowerShell 的 WScript.Shell COM 建 .lnk（可绑定图标，原生体验）。
macOS/Linux 退化成可执行脚本，不改无谓的封装——但图标要尽力给到：
macOS 的 .command 在 Finder 侧绑不了图标（只有 .app bundle 支持，而 .app 是独立
打包产物线），所以额外往桌面放一份 .icns 供手拖；Linux 按 freedesktop 规范写
`Icon=` 键。三级降级，任何一级缺失都不影响入口本身可用。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "gui" / "assets" / "mosu.ico"
# macOS 用 .icns（.command 在 Finder 侧绑不了图标，只能把 icns 放桌面让用户手拖）；
# Linux 用 .desktop 的 Icon= 键，优先绝对路径，回退 freedesktop 主题名。
ICON_PNG = ROOT / "gui" / "assets" / "logo-512.png"
ICON_ICNS = ROOT / "gui" / "assets" / "mosu.icns"
APP_NAME = "墨溯"
LINUX_ICON_NAME = "mosu"          # ~/.local/share/icons 下的文件名（不含扩展名）
LINUX_ICON_REL = ".local/share/icons/hicolor/512x512/apps"


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


# ---------------------------------------------------------------- 图标（三级降级）
def ensure_icns() -> bool:
    """由 logo-512.png 生成 macOS 的 .icns。返回是否成功。

    **绝不抛异常**：缺 Pillow 是正常情况（requirements 里 Pillow 是可选依赖），
    缺图标只影响观感，不该让「建快捷方式」失败。
    """
    if not ICON_PNG.exists():
        print(f"[i] 找不到图标源文件 {ICON_PNG.name}，macOS 入口将不带图标")
        return False
    try:
        from PIL import Image
    except Exception:
        print("[i] 未装 Pillow，无法生成 macOS 图标（pip install Pillow 可启用）")
        return False
    try:
        Image.open(ICON_PNG).save(ICON_ICNS, format="ICNS")
        return True
    except Exception as e:
        print(f"[i] 生成 {ICON_ICNS.name} 失败：{type(e).__name__}: {e}")
        return False


def _resolve_icons() -> dict:
    """图标可用性：{"png": Path|None, "icns": Path|None}。全缺则两个都是 None。"""
    return {"png": ICON_PNG if ICON_PNG.exists() else None,
            "icns": ICON_ICNS if ICON_ICNS.exists() else None}


def _install_linux_icon(icons: dict) -> str | None:
    """把 PNG 装进 ~/.local/share/icons 并返回 .desktop 里该写的 Icon= 值。

    三种结果：
      - 装了成功 → 绝对路径（最稳，不依赖图标主题缓存）
      - 装了失败（home 只读等）→ "mosu"（回退到 freedesktop 主题名，仍合法）
      - PNG 根本不存在 → None（调用方**不要写 Icon= 键**，缺键的 .desktop 仍合法）
    """
    png = icons.get("png")
    if not png:
        return None
    try:
        d = Path.home() / LINUX_ICON_REL
        d.mkdir(parents=True, exist_ok=True)
        dst = d / f"{LINUX_ICON_NAME}.png"
        shutil.copyfile(png, dst)
        return str(dst)
    except Exception as e:
        print(f"[i] 图标安装失败（{type(e).__name__}: {e}），改用主题名回退")
        return LINUX_ICON_NAME


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
    icons = _resolve_icons()
    if sys.platform == "darwin":
        f = d / f"{APP_NAME}.command"
        f.write_text(f'#!/bin/sh\nexec "{exe}" {arguments}\n', encoding="utf-8")
        f.chmod(0o755)
        made.append(str(f))
        # .command 在 Finder 侧无法绑图标（只有 .app bundle 支持），
        # 所以额外放一份 icns 到桌面，用户可自行改名/手拖
        icns = icons.get("icns")
        if icns:
            try:
                dst = d / f"{APP_NAME}.icns"
                shutil.copyfile(icns, dst)     # 同名覆盖，重复执行不产生副本
                made.append(str(dst))
                print(f"   图标：把桌面上的 {dst.name} 与入口一起拖进程序坞即可带图标")
            except Exception as e:
                print(f"[i] 复制 icns 到桌面失败：{type(e).__name__}: {e}")
    else:
        f = d / f"{APP_NAME}.desktop"
        icon_value = _install_linux_icon(icons)
        lines = [
            "[Desktop Entry]",
            "Type=Application",
            f"Name={APP_NAME}",
            "Comment=墨溯 · 微信数据本地控制台",
            f"Exec={exe} {arguments}",
            f"Path={ROOT}",
            "Terminal=false",
            "Categories=Utility;",
        ]
        # PNG 缺失时**整行都不写**：Icon= 空值会让部分桌面环境解析失败
        if icon_value:
            lines.append(f"Icon={icon_value}")
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        f.chmod(0o755)
        made.append(str(f))
    return made


def create_shortcut() -> int:
    if os.name != "nt" and sys.platform != "darwin" and not sys.platform.startswith("linux"):
        print("[x] 未知平台，跳过快捷方式")
        return 1
    if os.name != "nt":
        ensure_icns()          # macOS 的图标得先生成，Linux 用 PNG 不依赖它
    made = _shortcut_windows() if os.name == "nt" else _shortcut_posix()
    if not made:
        print("[x] 未能在任何桌面目录创建快捷方式")
        return 1
    for m in made:
        print(f"✅ 桌面快捷方式：{m}")
    print("   双击即可打开墨溯（服务未启动时会自动拉起）")
    return 0


def _remove_posix() -> int:
    """mac/linux：删桌面入口与桌面 icns。

    **不删** `~/.local/share/icons` 下那份——那是共享的图标安装目录，
    别的 .desktop 可能正引用它，删了会影响其他应用。
    """
    removed = 0
    d = Path.home() / "Desktop"
    names = ([f"{APP_NAME}.command", f"{APP_NAME}.icns"] if sys.platform == "darwin"
             else [f"{APP_NAME}.desktop", f"{APP_NAME}.icns"])
    for n in names:
        f = d / n
        if f.exists():
            try:
                f.unlink()
                print(f"✅ 已删除 {f}")
                removed += 1
            except Exception as e:
                print(f"[x] 删除失败 {f}：{e}")
    if not removed:
        print("[i] 桌面上没找到墨溯入口")
    else:
        print(f"   （{LINUX_ICON_REL}/{LINUX_ICON_NAME}.png 保留：共享图标目录，不动）")
    return 0


def remove_shortcut() -> int:
    if os.name != "nt":
        if sys.platform != "darwin" and not sys.platform.startswith("linux"):
            print("[i] 未知平台，请手动删除桌面上的墨溯入口")
            return 0
        return _remove_posix()
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
