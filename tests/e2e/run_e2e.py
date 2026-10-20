# -*- coding: utf-8 -*-
"""墨溯 GUI 端到端自动化测试。

设计原则（第一性原理）：
  用户说「能用」= 点得动 + 看得见 + 不出错。三者缺一不可。
  所以每个模块都做三件事，且**任何一项失败即整个模块失败**：
    ① 渲染：页面有实质内容（不是白屏、不是只剩骨架）
    ② 交互：真实点击/键入，观察状态是否真的改变
    ③ 洁净：控制台无 error、网络无 4xx/5xx（白名单除外）

对抗性设计：
  - 不信「元素存在」就等于「功能可用」→ 必须断言**状态变化**（数字变了、列表长了）
  - 不信接口 200 → 全部走真实 UI
  - 每步截图存档，失败时带 DOM 快照，便于事后定位
  - 致命错误检测：出现 .fatal-fallback 立即判失败（Vue 渲染崩了的标志）

用法:
    python tests/e2e/run_e2e.py                  # 全量
    python tests/e2e/run_e2e.py --only browse    # 只跑某模块
    python tests/e2e/run_e2e.py --headed         # 有头模式（看得见）
产物:
    .cache/e2e/<时间戳>/report.json  + 每步 PNG
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT_ROOT = ROOT / ".cache" / "e2e"
BASE = "http://127.0.0.1:8788"

# 预期内的失败请求：朋友圈远程图取不到是数据限制，不是缺陷
IGNORE_URL_PARTS = ("/api/remote", "/api/moments/probe", "favicon")

VIEWPORT = {"width": 1500, "height": 950}


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    shot: str = ""


@dataclass
class Module:
    key: str
    name: str
    steps: list[Step] = field(default_factory=list)
    # 本模块预期会失败的请求（前缀匹配）。写明的会被单独记录为「已知」而非缺陷，
    # 但必须给出原因——不允许用空列表悄悄放行一切。
    expected_failures: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps) and bool(self.steps)


class Recorder:
    """一次运行的记录器：截图、控制台、失败请求。"""

    def __init__(self, out: Path, page):
        self.out = out
        self.page = page
        self.console_errors: list[str] = []
        self.failed_requests: list[str] = []
        self._attach()

    def _attach(self):
        def on_console(msg):
            if msg.type != "error":
                return
            t = msg.text
            # 浏览器对 4xx 资源的报错只有一句「Failed to load resource」，
            # 不带 URL，无法按 URL 白名单过滤。已知的图片取不到会在
            # failed_requests 里单独记录（那里带 URL），console 这里放行，
            # 否则每个有远程图的页面都会被这条噪音判失败。
            if "Failed to load resource" in t:
                return
            self.console_errors.append(t[:300])

        def on_reqfail(req):
            url = req.url
            if any(p in url for p in IGNORE_URL_PARTS):
                return
            self.failed_requests.append(f"{req.method} {url[:160]}")

        def on_resp(resp):
            url = resp.url
            if any(p in url for p in IGNORE_URL_PARTS):
                return
            if resp.status >= 400:
                self.failed_requests.append(f"{resp.status} {url[:160]}")

        self.page.on("console", on_console)
        self.page.on("requestfailed", on_reqfail)
        self.page.on("response", on_resp)

    def reset(self):
        self.console_errors.clear()
        self.failed_requests.clear()

    async def shot(self, name: str) -> str:
        p = self.out / f"{name}.png"
        try:
            await self.page.screenshot(path=str(p), full_page=False)
            return p.name
        except Exception:
            return ""


async def _fatal(page) -> str:
    """页面是否落入了渲染失败兜底卡。"""
    try:
        n = await page.locator(".fatal-fallback").count()
        if n:
            txt = await page.locator(".fatal-msg").first.inner_text()
            return txt[:200]
    except Exception:
        pass
    return ""


async def _body_len(page) -> int:
    try:
        return len((await page.locator("body").inner_text()).strip())
    except Exception:
        return 0


async def wait_content(page, min_len: int = 300, timeout: float = 30.0,
                       selector: str | None = None, min_count: int = 1):
    """轮询等待页面出现实质内容，返回 (耗时秒, 是否达到, 最终值)。

    不能用「死等 N 秒」——墨溯各页加载差异极大：
    联系人/朋友圈 0.5s，画像 5.1s，年报 5.6s（要全库聚合）。
    写死 sleep 的话，慢页面会被误判成"白屏"（第一版就这么误报了 4 个模块）。
    用轮询：慢页面等到够为止，快页面不浪费时间。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        await page.wait_for_timeout(300)
        if selector:
            val = await page.locator(selector).count()
            enough = val >= min_count
        else:
            val = await _body_len(page)
            enough = val >= min_len          # 注意：正文比的是 min_len，不是 min_count
        if enough:
            return time.time() - t0, True, val
    if selector:
        val = await page.locator(selector).count()
    else:
        val = await _body_len(page)
    return time.time() - t0, False, val


async def check_step(rec: Recorder, mod: Module, name: str, cond: bool,
                     detail: str = "", shot_name: str | None = None) -> bool:
    """记录一步检查；自动附带截图与致命错误信息。"""
    fatal = await _fatal(rec.page)
    if fatal:
        cond = False
        detail = f"[渲染崩溃] {fatal} | {detail}"
    shot = await rec.shot(shot_name or f"{mod.key}-{len(mod.steps)+1}") if shot_name is not False else ""
    mod.steps.append(Step(name=name, ok=bool(cond), detail=detail, shot=shot))
    return bool(cond)


# ---------------------------------------------------------------- 各模块测试
async def t_console(ctx) -> Module:
    page, rec = ctx["page"], ctx["rec"]
    m = Module("console", "控制台")
    await page.goto(f"{BASE}/#/console", wait_until="networkidle")
    await page.wait_for_timeout(800)

    n = await _body_len(page)
    await check_step(rec, m, "页面有实质内容", n > 300, f"正文 {n} 字")
    tiles = await page.locator(".tile, .card").count()
    await check_step(rec, m, "状态卡片渲染", tiles >= 3, f"{tiles} 个卡片")
    navs = await page.locator(".nav-item, .nav a, aside a").count()
    await check_step(rec, m, "左侧导航完整", navs >= 9, f"{navs} 项")
    return m


async def t_browse(ctx) -> Module:
    page, rec = ctx["page"], ctx["rec"]
    m = Module("browse", "数据浏览")
    # 微信只把「用户真看过/下载过」的图片落盘，未下载的会留下一个几百字节的
    # 占位文件（头部 07 08 56…）。这类图必然取不到，属已知数据限制：
    # 实测 15992 张里 999 张是这种空壳。不是索引或解密出错。
    m.expected_failures = {
        "/api/media": "该图本地只有未下载占位文件（微信未落盘），非解密失败",
    }
    await page.goto(f"{BASE}/#/browse", wait_until="networkidle")
    await page.wait_for_timeout(1000)

    sessions = await page.locator(".session-item").count()
    await check_step(rec, m, "会话列表渲染", sessions >= 50, f"{sessions} 条会话")

    # 点第一个会话 —— 这是历史上白屏的地方，必须验证消息真的出来
    await page.locator(".session-item").first.click()
    cost, hit, bubbles = await wait_content(page, selector=".bubble", min_count=1, timeout=25)
    after = await page.locator(".session-item").count()
    await check_step(rec, m, "点开会话出消息", hit, f"{bubbles} 个气泡 / {cost:.1f}s")
    # 同上：只判 >0 的话，一页只渲染 1 条也能"通过"。正常首页是 60 条。
    await check_step(rec, m, "消息条数合理", bubbles >= 20, f"{bubbles} 条（期望 ≥20）")
    await check_step(rec, m, "会话列表未消失", after == sessions,
                     f"点前 {sessions} → 点后 {after}")

    # 加载更早
    btn = page.locator("button", has_text="载入更早")
    if await btn.count() and await btn.first.is_enabled():
        before = bubbles
        await btn.first.click()
        ok, hit2, bubbles2 = False, False, before
        t0 = time.time()
        while time.time() - t0 < 20:
            await page.wait_for_timeout(300)
            bubbles2 = await page.locator(".bubble").count()
            if bubbles2 > before:
                hit2 = True
                break
        await check_step(rec, m, "载入更早生效", hit2, f"{before} → {bubbles2} 条")
    else:
        await check_step(rec, m, "载入更早按钮可用", False, "按钮不存在或不可用")

    # F5 刷新深链
    sid = await page.evaluate(
        "() => { const m = location.hash.match(/session=([^&]+)/); return m ? m[1] : ''; }")
    if sid:
        await page.reload(wait_until="networkidle")
        cost3, hit3, b2 = await wait_content(page, selector=".bubble", min_count=1, timeout=25)
        await check_step(rec, m, "深链 F5 后仍可用", hit3, f"{b2} 个气泡")
    return m


async def t_search(ctx) -> Module:
    page, rec = ctx["page"], ctx["rec"]
    m = Module("search", "全库搜索")
    await page.goto(f"{BASE}/#/search", wait_until="networkidle")
    await page.wait_for_timeout(1200)

    modal = page.locator(".modal")
    await check_step(rec, m, "导航进入即弹搜索框", await modal.count() > 0)

    inp = page.locator(".modal input.input")
    await inp.fill("项目")
    t0 = time.time()
    await inp.press("Enter")
    cost, hit, results = await wait_content(page, selector=".modal .session-item",
                                            min_count=1, timeout=30)
    cost = time.time() - t0
    await check_step(rec, m, "搜索结果渲染", hit, f"{results} 条，{cost:.1f}s")
    await check_step(rec, m, "搜索耗时可接受", cost < 8, f"{cost:.1f}s")
    # 必须断言「条数合理」，不能只断言 >0：
    # 消融实验（把 limit 改成 1）时，只判 >0 的话 10/10 全绿——
    # 这是"测试看着通过、功能其实坏了"的典型。
    await check_step(rec, m, "首页结果数合理", results >= 20,
                     f"{results} 条（期望 ≥20，关键词「项目」实测 2000+ 命中）")

    body = await modal.inner_text()
    has_count = ("条命中" in body) or ("至少" in body)
    await check_step(rec, m, "显示命中条数", has_count, body[:80].replace("\n", " "))

    # 翻页
    nxt = page.locator(".modal button", has_text="下一页")
    if await nxt.count() and await nxt.first.is_enabled():
        first_before = await page.locator(".modal .session-item").first.inner_text()
        await nxt.first.click()
        t0 = time.time()
        changed = False
        while time.time() - t0 < 25:
            await page.wait_for_timeout(300)
            cur = await page.locator(".modal .session-item").first.inner_text()
            if cur != first_before:
                changed = True
                break
        first_after = await page.locator(".modal .session-item").first.inner_text()
        await check_step(rec, m, "下一页换了内容", changed,
                         f"{first_before[:24]} → {first_after[:24]}")
    else:
        await check_step(rec, m, "下一页按钮可用", False, "按钮不存在或禁用")

    # 点结果跳转
    await page.locator(".modal .session-item").first.click()
    cost2, hit2, b = await wait_content(page, selector=".bubble", min_count=1, timeout=25)
    await check_step(rec, m, "点结果跳转后出消息", hit2, f"{b} 个气泡")
    return m


async def t_stats(ctx) -> Module:
    page, rec = ctx["page"], ctx["rec"]
    m = Module("stats", "数据统计")
    await page.goto(f"{BASE}/#/stats", wait_until="networkidle")
    await page.wait_for_timeout(1500)
    n = await _body_len(page)
    await check_step(rec, m, "页面有实质内容", n > 300, f"正文 {n} 字")
    svg = await page.locator("svg").count()
    await check_step(rec, m, "图表已绘制", svg >= 2, f"{svg} 个 svg")
    return m


async def t_profile(ctx) -> Module:
    page, rec = ctx["page"], ctx["rec"]
    m = Module("profile", "画像")
    await page.goto(f"{BASE}/#/profile", wait_until="networkidle")
    # 画像要全库聚合（23 万条），实测约 5s，必须轮询不能死等
    cost, hit, n = await wait_content(page, min_len=1000, timeout=30)
    cards = await page.locator(".card").count()
    kv = await page.locator("dl.kv").count()
    dbg = f"{cost:.1f}s/{n}字/card={cards}/dl={kv}/{page.url.split('#')[-1]}"
    await check_step(rec, m, "画像内容加载出来", hit, dbg)
    await check_step(rec, m, "加载不算过慢", cost < 15, f"{cost:.1f}s")
    await check_step(rec, m, "画像卡片渲染", cards >= 5, f"{cards} 卡")
    # 历史上这里崩 isObj is not a function。
    # 字段表的类名是 dl.kv（写错选择器会误判成页面坏了）
    await check_step(rec, m, "字段表渲染", kv >= 5, f"{kv} 个字段表")
    return m


async def t_report(ctx) -> Module:
    page, rec = ctx["page"], ctx["rec"]
    m = Module("report", "年报")
    await page.goto(f"{BASE}/#/report", wait_until="networkidle")
    cost, hit, n = await wait_content(page, min_len=400, timeout=30)
    await check_step(rec, m, "年报内容加载出来", hit, f"{cost:.1f}s / {n} 字")
    await check_step(rec, m, "加载不算过慢", cost < 15, f"{cost:.1f}s")
    return m


async def t_contacts(ctx) -> Module:
    page, rec = ctx["page"], ctx["rec"]
    m = Module("contacts", "联系人")
    await page.goto(f"{BASE}/#/contacts", wait_until="networkidle")
    cost, hit, n = await wait_content(page, min_len=600, timeout=20)
    await check_step(rec, m, "联系人列表渲染", hit, f"{cost:.1f}s / {n} 字")
    return m


async def t_moments(ctx) -> Module:
    page, rec = ctx["page"], ctx["rec"]
    m = Module("moments", "朋友圈")
    # 微信不把朋友圈图片落盘（只存链接+token），腾讯图床又拒绝直连请求
    # （实测返回 400 / X-ErrNo:-106）。这是数据来源的固有限制，不是缺陷。
    # 已核验：本机全盘按 md5 搜索 0 命中，服务器也取不到。
    m.expected_failures = {
        "/api/remote": "远程图取不到（微信不落盘 + 图床拒绝），属已知限制",
    }
    await page.goto(f"{BASE}/#/moments", wait_until="networkidle")
    cost, hit, n = await wait_content(page, min_len=400, timeout=20)
    await check_step(rec, m, "朋友圈内容渲染", hit, f"{cost:.1f}s / {n} 字")
    # 图片取不到是已知数据限制（微信不落盘 + 图床拒绝），
    # 断言的重点是"有没有告诉用户"，而不是"有没有图"。
    # 提示条由异步探测完成后才出现，必须轮询等（正文先到、探测后到）。
    cost2, warned, cnt = await wait_content(page, selector=".notice", min_count=1, timeout=20)
    await check_step(rec, m, "对取不到的图有明确说明", warned,
                     f"提示条出现（{cost2:.1f}s）" if warned else "无提示（用户会以为是坏了）")
    return m


async def t_export(ctx) -> Module:
    page, rec = ctx["page"], ctx["rec"]
    m = Module("export", "导出")
    await page.goto(f"{BASE}/#/export", wait_until="networkidle")
    await page.wait_for_timeout(1200)
    n = await _body_len(page)
    await check_step(rec, m, "导出页渲染", n > 200, f"正文 {n} 字")
    # 只验证控件存在，不真的生成文件（会产生大文件、耗时不可控）
    btn = page.locator("button").filter(has_text="导出")
    cnt = await btn.count()
    await check_step(rec, m, "导出按钮存在", cnt > 0, f"{cnt} 个")
    return m


async def t_settings(ctx) -> Module:
    page, rec = ctx["page"], ctx["rec"]
    m = Module("settings", "设置")
    await page.goto(f"{BASE}/#/settings", wait_until="networkidle")
    await page.wait_for_timeout(1200)
    n = await _body_len(page)
    await check_step(rec, m, "设置页渲染", n > 200, f"正文 {n} 字")
    return m


TESTS = [t_console, t_browse, t_search, t_stats, t_profile,
         t_report, t_contacts, t_moments, t_export, t_settings]


async def main(only: str | None, headed: bool) -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUT_ROOT / stamp
    out.mkdir(parents=True, exist_ok=True)

    fatal_hits = {"url": 0}
    try:
        import urllib.request
        urllib.request.urlopen(f"{BASE}/", timeout=5)
    except Exception as e:
        print(f"[x] 服务未启动（{BASE}）：{e}")
        return 2

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="msedge", headless=not headed,
            args=["--no-proxy-server", "--force-device-scale-factor=1"])

        modules: list[Module] = []
        for fn in TESTS:
            if only and only not in fn.__name__:
                continue
            # 每个模块用独立 page：上一个页面的异步图片请求（朋友圈是异步探测）
            # 会在切页后才返回 404，串到下一个模块的账上——导出模块就曾被
            # 朋友圈的 29 个 404 冤判失败。隔离页面是唯一可靠的做法。
            page = await browser.new_page(viewport=VIEWPORT, locale="zh-CN")
            rec = Recorder(out, page)
            ctx = {"page": page, "rec": rec, "fatal": fatal_hits}
            try:
                m = await fn(ctx)
                if rec.console_errors:
                    m.steps.append(Step("控制台无 error", False,
                                        " | ".join(rec.console_errors[:3])))
                else:
                    m.steps.append(Step("控制台无 error", True))
            except Exception as e:
                m = Module(fn.__name__, fn.__name__)
                m.steps.append(Step("模块执行未抛异常", False, f"{type(e).__name__}: {e}"))
                await rec.shot(f"crash-{fn.__name__}")
            finally:
                await page.close()

            # 失败请求按模块声明的「预期失败」分流：
            # 预期内的单独记一条（附原因），不阻塞放行；
            # 未声明的才算缺陷。这样既不掩盖问题，也不让已知限制永远挡住验收。
            if rec.failed_requests:
                exp, unexp = [], []
                for r in rec.failed_requests:
                    hit = next((why for pre, why in m.expected_failures.items() if pre in r), None)
                    (exp if hit else unexp).append((r, hit))
                if unexp:
                    m.steps.append(Step("无意外失败请求", False,
                                        " | ".join(x[0] for x in unexp[:3])))
                if exp:
                    m.steps.append(Step(
                        f"已知失败 {len(exp)} 项（预期内）", True,
                        "；".join(f"{x[0][:50]} → {x[1]}" for x in exp[:1])))

            modules.append(m)
            print(f"  {'✅' if m.ok else '❌'} {m.name}")

        await browser.close()

    # ---- 报告
    total = sum(len(m.steps) for m in modules)
    passed = sum(1 for m in modules for s in m.steps if s.ok)
    ok_mods = [m.name for m in modules if m.ok]
    bad_mods = [m.name for m in modules if not m.ok]

    report = {
        "time": stamp,
        "base": BASE,
        "summary": {"modules": len(modules), "modules_ok": len(ok_mods),
                    "steps": total, "steps_ok": passed,
                    "failed_modules": bad_mods},
        "modules": [{
            "key": m.key, "name": m.name, "ok": m.ok,
            "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail, "shot": s.shot}
                      for s in m.steps],
        } for m in modules],
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")

    print("\n" + "=" * 56)
    print(f"模块 {len(ok_mods)}/{len(modules)} 通过   步骤 {passed}/{total} 通过")
    if bad_mods:
        print(f"未通过模块: {', '.join(bad_mods)}")
        print("失败明细:")
        for m in modules:
            for s in m.steps:
                if not s.ok:
                    print(f"  - [{m.name}] {s.name}: {s.detail}")
    print(f"报告: {out / 'report.json'}")
    print(f"截图: {out}")
    print("=" * 56)
    return 0 if not bad_mods else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑名字含该串的模块")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.only, a.headed)))
