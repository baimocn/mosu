"""由 gui/assets/logo.svg 生成多尺寸 ico（Windows 快捷方式 / 任务栏 / 文件图标）。

流程：Edge 无头渲染 SVG → 512px PNG → PIL 缩放出各尺寸 → 打包 ICO。
零第三方构建依赖，只需要 Pillow（可选，缺失时降级为不生成 ico）。
用法：python scripts/make_icon.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "gui" / "assets" / "logo.svg"
ICO = ROOT / "gui" / "assets" / "mosu.ico"
PNG = ROOT / "gui" / "assets" / "logo-512.png"
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    shutil.which("msedge") or "",
    shutil.which("google-chrome") or "",
]


def _find_edge() -> str | None:
    for c in EDGE_CANDIDATES:
        if c and Path(c).exists():
            return c
    return None


def render_png(out: Path) -> bool:
    """用无头浏览器把 SVG 渲染成透明底 PNG。找不到浏览器就跳过（不硬失败）。"""
    edge = _find_edge()
    if not edge:
        print("[i] 没找到 Edge/Chrome，跳过渲染；ico 保持原样")
        return False
    uri = SVG.resolve().as_uri()
    # 注意：subprocess 传列表时不能自己加引号（那是 shell 的活），
    # 否则路径里会带引号，浏览器写不出文件。
    cmd = [edge, "--headless=new", "--disable-gpu", "--no-proxy-server",
           "--default-background-color=00000000",
           f"--screenshot={out.resolve()}",
           "--window-size=512,512", "--virtual-time-budget=6000", uri]
    try:
        subprocess.run(cmd, capture_output=True, timeout=90)
    except Exception as e:
        print(f"[x] 渲染失败：{type(e).__name__}: {e}")
        return False
    return out.exists() and out.stat().st_size > 1024


def main() -> int:
    if not SVG.exists():
        print(f"[x] 找不到 {SVG}")
        return 1
    if not render_png(PNG):
        return 0
    try:
        from PIL import Image
    except Exception:
        print("[i] 没有 Pillow，无法打包 ico")
        return 0
    base = Image.open(PNG).convert("RGBA")
    base.save(ICO, format="ICO", sizes=[(s, s) for s in SIZES], bitmap_format="png")
    print(f"✅ {ICO} ({ICO.stat().st_size} bytes, {len(SIZES)} sizes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())