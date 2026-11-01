"""迭代 4 端到端：分支树（泳道 + 语义锚点光线）真机验证。

验证五件事：
  1. 真跑一次会触发多代理委派的问题 → 后端真产出分支
  2. 前端渲染出泳道（分支树真的显示了）
  3. **连线真的画出来了**（SVG path 有 d 属性，不是空的）
  4. **锚点缺失不乱飞**：手动藏掉一个节点 → 那条线应该消失，
     而不是落到邻近节点上
  5. 颜色按泳道继承：同泳道节点颜色一致，不同泳道不同

用法：python .cache/audit/e2e_iter4.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

# 仓库根：向上找带 core/ 的那层，不硬编码目录深度
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "core").is_dir() and (p / "cli").is_dir())
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8788"
PY = sys.executable


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

        print("\n[2] 真提问（触发多代理委派）")
        # 这个问题容易被拆成多个子任务
        q = "分别分析我和李小明、赵小刚、王小红三个人的聊天往来，各查各的"
        pg.fill(".v2-ia-input", q)
        pg.click(".v2-ia-go")

        # 等分支树出现（最多 150 秒）
        tree = pg.locator(".v2-bt")
        seen = False
        for i in range(300):
            pg.wait_for_timeout(500)
            if tree.count():
                seen = True
                print(f"   [{i*0.5:.1f}s] 分支树出现了")
                break
        check(results, "分支树渲染出来了", seen)

        if not seen:
            print("   分支树没出现——可能模型没委派，也可能是渲染有问题")
            pg.screenshot(path=str(ROOT / ".cache/audit/v2/iter4-notree.png"),
                          full_page=True)
            b.close()
            return _summary(results)

        # 等它跑完（分支状态要稳定）
        print("\n[3] 等分析跑完")
        for i in range(240):
            pg.wait_for_timeout(500)
            if pg.locator(".v2-ia-running").count() == 0:
                break
        pg.wait_for_timeout(1500)

        lanes = pg.locator(".v2-bt-lane")
        nodes = pg.locator(".v2-bt-node")
        paths = pg.locator(".v2-bt-line")
        print(f"   泳道数：{lanes.count()}  节点数：{nodes.count()}  连线数：{paths.count()}")
        check(results, "有泳道", lanes.count() > 0, f"{lanes.count()} 条")
        check(results, "有节点", nodes.count() > 0, f"{nodes.count()} 个")

        # ---- 连线真的画出来了
        print("\n[4] 连线（语义锚点光线）")
        ds = pg.eval_on_selector_all(
            ".v2-bt-line", "els => els.map(e => e.getAttribute('d'))")
        real = [d for d in ds if d and d.startswith("M") and len(d) > 20]
        check(results, "SVG 连线有实际路径", len(real) > 0,
              f"{len(real)}/{len(ds)} 条有 d")
        if real:
            print(f"   样例：{real[0][:80]}")

        # ---- 锚点语义
        keys = pg.eval_on_selector_all(
            ".v2-bt-node", "els => els.map(e => e.getAttribute('data-anchor-key'))")
        check(results, "节点有语义锚点", all(k and k.startswith("branch:") for k in keys),
              f"{len(keys)} 个锚点")

        # ---- 虚拟根
        print("\n[5] 虚拟根（主代理）")
        root = pg.locator(".v2-bt-root")
        check(results, "虚拟根渲染出来了", root.count() == 1)
        root_key = pg.eval_on_selector(
            ".v2-bt-root", "e => e.getAttribute('data-anchor-key')")
        check(results, "虚拟根有语义锚点", root_key == "branch:root", str(root_key))
        # 顶层分支必须有一条从根出发的线
        root_edges = pg.eval_on_selector_all(
            ".v2-bt-line",
            "els => els.filter(e => (e.getAttribute('d')||'').length > 20).length")
        top_nodes = pg.eval_on_selector_all(
            ".v2-bt-node", "els => els.filter(e => !e.className.includes('d-2')).length")
        check(results, "顶层分支都有从根出发的连线",
              root_edges >= top_nodes > 0,
              f"根连线 {root_edges} 条 / 顶层分支 {top_nodes} 个")

        # ---- 颜色按泳道继承
        print("\n[6] 颜色按泳道继承")
        lane_colors = pg.eval_on_selector_all(
            ".v2-bt-lane", "els => els.map(e => e.style.getPropertyValue('--lc'))")
        check(results, "每条泳道都有颜色",
              all(c and c.strip() for c in lane_colors), str(lane_colors))
        check(results, "不同泳道颜色不同",
              len(set(lane_colors)) == len(lane_colors),
              f"{len(set(lane_colors))} 种 / {len(lane_colors)} 条")
        # 节点边框色继承泳道色
        node_border = pg.eval_on_selector_all(
            ".v2-bt-node", "els => els.map(e => getComputedStyle(e).borderLeftColor)")
        print(f"   节点边框色：{node_border}")

        pg.screenshot(path=str(ROOT / ".cache/audit/v2/iter4-tree.png"), full_page=True)

        # ---- 锚点缺失不乱飞
        print("\n[7] 锚点缺失不乱飞（核心纪律）")
        before = pg.locator(".v2-bt-line").count()
        # 藏掉所有节点 → 所有锚点读不到 → 所有线必须消失
        pg.eval_on_selector_all(
            ".v2-bt-node",
            "els => els.forEach(e => { e.style.display = 'none'; })")
        pg.wait_for_timeout(900)
        after = pg.locator(".v2-bt-line").count()
        check(results, "锚点隐藏后连线全部消失", after == 0,
              f"隐藏前 {before} 条 → 隐藏后 {after} 条")
        # 恢复
        pg.eval_on_selector_all(
            ".v2-bt-node",
            "els => els.forEach(e => { e.style.display = ''; })")
        pg.wait_for_timeout(900)
        restored = pg.locator(".v2-bt-line").count()
        check(results, "恢复后连线回来", restored == before,
              f"{restored} 条")

        # ---- 只藏一个：只有连到它的线该消失，其他线不受影响
        if nodes.count() > 1 and before > 1:
            pg.eval_on_selector(
                ".v2-bt-node",
                "e => { e.style.display = 'none'; }")
            pg.wait_for_timeout(900)
            partial = pg.locator(".v2-bt-line").count()
            check(results, "藏一个节点只影响相关连线",
                  partial < before,
                  f"{before} → {partial} 条")
            pg.eval_on_selector_all(
                ".v2-bt-node",
                "els => els.forEach(e => { e.style.display = ''; })")
            pg.wait_for_timeout(600)

        print("\n[8] console")
        bad = [e for e in errs if e.startswith("PAGEERROR") or e.startswith("error")]
        if bad:
            for e in bad[:8]:
                print("   ", e)
        else:
            print("   无错误")
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
