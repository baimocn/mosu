# -*- coding: utf-8 -*-
"""生成 README 用的界面截图（脱敏模式）。

必须配合 `MOSU_DEMO=1` 启动的 GUI：
    set MOSU_DEMO=1 && python cli/mosu.py gui --port 8788
    python scripts/shots.py

为什么单独一个脚本而不是手动截图：
  1. 要固定视口和状态，手截每次都不一样，README 里几张图风格会打架；
  2. 要**自动检查**图里有没有残留真实信息——肉眼漏掉一次，
     公开仓库就永久留痕了；
  3. 换 UI 之后能一键重出，不用回忆上次怎么截的。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "shots"
BASE = os.environ.get("MOSU_SHOT_BASE", "http://127.0.0.1:8788")
VIEWPORT = {"width": 1440, "height": 900}

# (文件名, 路由, 说明, 需要的额外等待秒数)
SHOTS = [
    ("01-console.png", "#/console", "控制台", 2.0),
    ("02-browse.png", "#/browse", "数据浏览", 2.5),
    ("03-search.png", "#/search", "全库搜索", 1.5),
    ("04-stats.png", "#/stats", "数据统计", 3.0),
    ("05-profile.png", "#/profile", "个人画像", 8.0),
    ("06-report.png", "#/report", "年度报告", 8.0),
    ("07-contacts.png", "#/contacts", "联系人", 2.0),
    ("08-moments.png", "#/moments", "朋友圈", 4.0),
    ("09-export.png", "#/export", "导出", 1.5),
    ("10-settings.png", "#/settings", "设置", 1.5),
]

# 这些字符串一旦出现在页面上，说明脱敏没生效——直接判失败，不出图。
FORBIDDEN = [
    "C:\\Users\\",
    "D:\\wechat-data",
    "@im.wechat",
    "35955",
    "MOSU_KEY_HEX =",
]

# 正则类：真实 wxid / 群号。
# 注意 demo 模式会生成 wxid_demo1234 这类假 ID，不能一刀切禁 "wxid_"，
# 否则每张图都误报（第一版就是这样，4 个正常页面被判失败）。
FORBIDDEN_RE = [
    (re.compile(r"wxid_(?!demo)[a-z0-9]{6,}"), "真实 wxid"),
    # 真群号是纯数字；demo 群号形如 demo12345678@chatroom，
    # 用 (?!demo) 排除，否则每张有群的页面都会误报
    (re.compile(r"(?<!demo)\b\d{9,}@chatroom"), "真实群号"),
]


async def wait_ready(page, timeout=25.0) -> bool:
    """等页面出实质内容。各页耗时差异大（画像 5s、年报 5.6s），必须轮询。"""
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        await page.wait_for_timeout(300)
        try:
            if len((await page.locator("body").inner_text()).strip()) > 400:
                return True
        except Exception:
            pass
    return False


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    # ---- 前置：确认 8788 上跑的是演示模式。
    # 本机有个计划任务 \MosuGuiNow 会拉起真实模式的 GUI，
    # 一旦它抢到端口，截出来的图里全是真实姓名和账号——
    # 而且页面看着完全正常，肉眼发现不了。所以必须硬检查。
    import urllib.request
    # 本机环境有系统代理，127.0.0.1 会被劫持成 502；必须显式绕开
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with _opener.open(f"{BASE}/api/status", timeout=8) as r:
            st = json.loads(r.read())
        acc = (st.get("wechat") or {}).get("account", "")
        if acc != "演示账号":
            print(f"[x] 8788 上跑的不是演示模式（account={acc!r}）")
            print("    先启动：python scripts/demo_server.py")
            print("    并确保计划任务 \\MosuGuiNow 没有把它挤掉")
            return 2
        print(f"[mosu] 演示模式已确认（account={acc}）\n")
    except Exception as e:  # noqa: BLE001
        print(f"[x] 连不上 {BASE}：{e}")
        return 2

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="msedge", headless=True,
            args=["--no-proxy-server", "--force-device-scale-factor=2"])
        ctx = await browser.new_context(viewport=VIEWPORT, locale="zh-CN",
                                        device_scale_factor=2)
        page = await ctx.new_page()

        for name, route, label, extra in SHOTS:
            await page.goto(f"{BASE}/{route}", wait_until="networkidle")
            ok = await wait_ready(page)
            if not ok:
                problems.append(f"{label}: 内容没加载出来（>{25}s）")
                print(f"  ❌ {label}: 加载超时")
                continue

            # 搜索页要真的搜一次，否则截到的是空框
            if route == "#/search":
                try:
                    inp = page.locator(".modal input.input")
                    await inp.fill("项目")
                    await inp.press("Enter")
                    await page.wait_for_timeout(3000)
                except Exception:
                    problems.append(f"{label}: 搜索框没弹出")

            await page.wait_for_timeout(int(extra * 1000))

            # ---- 脱敏检查（出图前，必须过）
            body = await page.locator("body").inner_text()
            leaks = [f for f in FORBIDDEN if f in body]
            for rx, kind in FORBIDDEN_RE:
                m = rx.search(body)
                if m:
                    leaks.append(f"{kind}: {m.group(0)}")
            if leaks:
                problems.append(f"{label}: 页面仍有敏感信息 {leaks}")
                print(f"  ❌ {label}: 泄漏 {leaks}")
                continue

            await page.screenshot(path=str(OUT / name), full_page=False)
            size_kb = (OUT / name).stat().st_size // 1024
            print(f"  ✅ {label:8s} → docs/shots/{name}  ({size_kb} KB)")

        await browser.close()

    if problems:
        print("\n未通过：")
        for x in problems:
            print(f"  - {x}")
        return 1
    print(f"\n全部生成完成：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
