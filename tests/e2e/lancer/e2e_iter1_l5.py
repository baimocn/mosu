"""迭代 1 的 L5 证据补测：多代理并行编排的真机验证。

为什么要补
----------
迭代 1 交付了「多代理并行编排」（core/agent_team.py），但当时只在
**真实数据 + 脚本**层面验过（L4），从没在真机上独立验过这个功能本身（L5）。

迭代 4 的端到端顺带覆盖了「分支树渲染」，但那是**分支树这个视图**的证据，
不是「多代理并行」这个**能力**的证据——两码事。
按新定的分级纪律（L4 不能替代 L5），这个缺口必须显式补上。

验什么
------
1. 真机提问 → 主代理真的**拆出多个子任务**并派出去（不是自己串行做完）
2. 多个子代理**真的并行**（墙钟时间明显小于串行耗时之和）
3. 每个子代理出**独立结论**（不是同一份答案复制三遍）
4. 分支树在真机上渲染，且泳道数 == 委派出的子代理数
5. 洞察页明确告诉用户「派了 N 个子代理去查」

用法：python .cache/audit/e2e_iter1_l5.py
"""
import sys
import time
from pathlib import Path

# 仓库根：向上找带 core/ 的那层，不硬编码目录深度
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "core").is_dir() and (p / "cli").is_dir())
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8788"


def check(results, name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def main():
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1440, "height": 960})
        errs = []
        pg.on("pageerror", lambda e: errs.append("PAGEERROR: " + str(e)))
        pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}")
              if m.type == "error" else None)

        print("[1] 打开洞察页")
        pg.goto(BASE + "/#/insight", wait_until="networkidle")
        pg.wait_for_timeout(2500)
        check(results, "洞察页加载", pg.locator(".v2-ia").count() > 0)

        print("\n[2] 真提问（应触发多代理委派）")
        # 明确要求分开查三个人 —— 模型最容易在这里拆子任务
        q = "分别分析我和李小明、赵小刚、王小红三个人的聊天往来，各查各的"
        t0 = time.time()
        pg.fill(".v2-ia-input", q)
        pg.click(".v2-ia-go")

        tree = pg.locator(".v2-bt")
        seen = False
        for i in range(300):
            pg.wait_for_timeout(500)
            if tree.count():
                seen = True
                print(f"   [{i*0.5:.1f}s] 分支树出现了")
                break
        check(results, "真机出现分支树（多代理被触发）", seen)
        if not seen:
            pg.screenshot(path=str(ROOT / ".cache/audit/v2/iter1-l5-notree.png"),
                          full_page=True)
            b.close()
            return _summary(results)

        print("\n[3] 等并行跑完")
        for i in range(240):
            pg.wait_for_timeout(500)
            if pg.locator(".v2-ia-running").count() == 0:
                break
        pg.wait_for_timeout(2000)
        wall = time.time() - t0

        lanes = pg.locator(".v2-bt-lane")
        nodes = pg.locator(".v2-bt-node")
        print(f"   泳道数：{lanes.count()}  节点数：{nodes.count()}  墙钟：{wall:.1f}s")
        check(results, "派出了多个子代理（泳道 > 1）", lanes.count() > 1,
              f"{lanes.count()} 条泳道")

        # 每个子代理的结论必须**独立**（不是复制粘贴）
        reports = pg.eval_on_selector_all(
            ".v2-bt-node-report",
            "els => els.map(e => e.textContent.trim()).filter(t => t.length > 0)")
        check(results, "每个子代理都出了结论", len(reports) >= 2,
              f"{len(reports)} 份报告")
        if len(reports) >= 2:
            uniq = len(set(reports))
            check(results, "子代理结论互相独立（不是复制）", uniq == len(reports),
                  f"{uniq} 种 / {len(reports)} 份")
            # 每份报告里的会话名应当不同
            names = []
            for r in reports:
                for n in ("李小明", "赵小刚", "王小红"):
                    if n in r:
                        names.append(n)
                        break
            check(results, "不同子代理查的是不同对象", len(set(names)) >= 2,
                  str(names))

        # 并行的核心证据：墙钟明显小于各子代理耗时之和
        costs = pg.eval_on_selector_all(
            ".v2-bt-node-meta",
            "els => els.map(e => (e.textContent.match(/(\\d+)\\s*秒/) || [])[1])")
        costs = [int(c) for c in costs if c and c.isdigit()]
        if costs:
            serial = sum(costs)
            print(f"   子代理耗时合计 {serial}s，墙钟 {wall:.1f}s")
            check(results, "并行收益存在（墙钟 < 串行耗时和）", wall < serial,
                  f"{wall:.1f}s vs {serial}s")

        print("\n[4] 用户能看到「派了 N 个子代理」")
        head = pg.locator(".v2-ia-steps-title")
        txts = [t.strip() for t in head.all_inner_texts()]
        multi = [t for t in txts if "子代理" in t]
        check(results, "洞察页明示派了子代理", len(multi) > 0, str(multi[:2]))

        print("\n[5] 分支树与子代理数一致")
        check(results, "泳道数 == 分支节点数",
              lanes.count() == nodes.count(),
              f"{lanes.count()} 泳道 / {nodes.count()} 节点")

        pg.screenshot(path=str(ROOT / ".cache/audit/v2/iter1-l5.png"), full_page=True)

        print("\n[6] console")
        bad = [e for e in errs if e.startswith("PAGEERROR") or e.startswith("error")]
        for e in bad[:8]:
            print("   ", e)
        check(results, "无 JS 错误", not bad, f"{len(bad)} 条")

        b.close()
    return _summary(results)


def _summary(results):
    print("\n[汇总]")
    passed = sum(1 for _, ok in results if ok)
    print(f"  {passed}/{len(results)} 通过")
    for name, ok in results:
        if not ok:
            print(f"  ❌ {name}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
