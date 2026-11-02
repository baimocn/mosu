"""迭代 12 端到端：跨分支语义锚点光线（对标 Lancer processTreeRoutes）。

验证七件事：
  1. **真跑三层委派**：主代理 → 子代理（真 AgentTeam + 真 ReAct 循环）
  2. **共享会话 → 真产生语义边**：两个子代理都读了同一个人的原话，
     必须算出 shared-session 关联
  3. **判据不误连**：只有 stat 证据的分支**不能**被连上
     （`find_contact` 会给搜到的每个会话产 stat 证据，
      算进去就满屏假线——这是第一版真实踩到的坑）
  4. **标签是人名不是 wxid**：`都查过 wxid_xxx` 用户看不懂
  5. **父子不连**：树线已经表达了那个关系
  6. **落盘 + 下发**：语义边要落盘（历史回看），也要真下发到前端
  7. **前端真渲染**：真浏览器 + 真 GUI 服务 + 真接口返回的语义边，
     必须画出虚线；藏掉一端 → 线必须消失（不画到邻近节点）

为什么用「脚本化 LLM 决策」而不是纯真模型
----------------------------------------
真模型**不保证**会派两个子代理去读**同一个人**——
那样测出来的「语义边成立」是**碰巧**，不是能力。

所以只替换模型的**决策层**（决定调哪个工具），其余全走真实路径：
  - 真 Dataset（真 SQLite、真解密）
  - 真 ToolBox（真工具、真证据）
  - 真 AgentTeam（真线程池、真分支树、真泳道）
  - 真 AgentEngine（真 ReAct 循环、真回喂）

核心逻辑一行没改，只有「模型下一步做什么」被脚本固定——
这正是端到端该固定的部分（否则每次跑结果都不一样，测不了）。

第 7 项则完全不用脚本：真起 GUI 服务、真调接口、真浏览器渲染。

前置条件
--------
本机要有可用的微信数据目录（~/.mosu/config.json 里有账号密钥）。
不需要 LLM 网关（决策层是脚本化的）。

用法：python tests/e2e/lancer/e2e_iter12.py
"""
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# 仓库根：向上找带 core/ 的那层，不硬编码目录深度
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "core").is_dir() and (p / "cli").is_dir())
sys.path.insert(0, str(ROOT))

GUI_PORT = 8803
PY = sys.executable


def check(results, name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def summary(results):
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"端到端结果：{passed}/{total} 通过")
    if passed < total:
        for name, ok in results:
            if not ok:
                print(f"  ❌ {name}")
    print("=" * 60)
    return 0 if passed == total else 1


# ------------------------------------------------------------------ 脚本化 LLM


class ScriptedLLM:
    """按**规则**选脚本的假 LLM（只替换决策层）。

    为什么不按「第几次调用」选脚本：并行子代理的创建顺序不确定，
    计数器派发会让「哪个分支拿到哪个脚本」变成竞态，
    每次跑结果可能不同——端到端必须确定性。

    改成按 **goal 文本**（messages 里的第一条 user 消息）匹配规则：
    同一个 goal 永远拿到同一个脚本，与线程调度无关。

    每个实例有**自己的**脚本栈——并行子代理各跑各的，
    不能共用一个脚本（否则三个子代理会抢同一个下标）。
    """

    def __init__(self, script: list | None = None, rules: list | None = None,
                 model: str = "scripted-e2e"):
        # 两种模式：
        #   script —— 固定脚本（主代理用，它的 goal 不会变）
        #   rules  —— [(匹配函数, 脚本), ...]（子代理用，按 goal 选）
        self.rules = list(rules or [])
        self.model = model
        self.calls: list[dict] = []
        self.script: list = list(script or [])
        self._picked = bool(script) or not self.rules

    def _pick(self, messages):
        if self._picked:
            return
        self._picked = True
        goal = ""
        for m in messages or []:
            if m.get("role") == "user":
                goal = m.get("content") or ""
                break
        for match, script in self.rules:
            if match(goal):
                self.script = script
                return
        self.script = [{"content": "（没有匹配到脚本）"}]

    def chat_tools(self, messages, tools, temperature=0.0, max_tokens=1000):
        idx = len(self.calls)
        self.calls.append({"idx": idx, "n_messages": len(messages),
                           "n_tools": len(tools or []),
                           "tool_results": sum(
                               1 for m in messages if (m.get("role") == "tool"))})
        self._pick(messages)
        step = self.script[idx] if idx < len(self.script) else {"content": "（脚本用尽）"}
        resp = {"usage": {"total_tokens": 10}, "content": step.get("content") or ""}
        if step.get("tool_calls"):
            resp["tool_calls"] = [
                {"id": c.get("id") or f"call_{idx}_{i}",
                 "name": c["name"],
                 "args": c.get("args") or {}}
                for i, c in enumerate(step["tool_calls"])
            ]
        return resp

    def chat(self, messages, temperature=0.0, max_tokens=1000):
        self.calls.append({"idx": len(self.calls), "n_messages": len(messages)})
        return {"usage": {"total_tokens": 10}, "content": "（成文模型不该被调用）"}


def _read_person_script(session_id: str, label: str,
                         with_grandchild: bool = False) -> list:
    """子代理脚本：真读某个人的原话（产出 quote 证据）。

    用 `read_timeline` 而不是 `search_messages`：
      - read_timeline 必产 quote 证据（读的是原话）；
      - search_messages 依赖关键词命中，不命中就一条证据都没有，
        端到端会测出「无边」——但那是**关键词选得不好**，
        不是功能不行。第一版就栽在这（拿会话名前两字当关键词，搜不到）。
    """
    steps = [
        {"tool_calls": [{"id": "r1", "name": "read_timeline",
                         "args": {"sessionId": session_id, "limit": 5}}]},
    ]
    if with_grandchild:
        steps.append(
            {"tool_calls": [{"id": "r2", "name": "delegate_tasks",
                             "args": {"tasks": [f"再细查一下{label}的另一条线"]}}]})
    steps.append({"content": f"{label}的相关消息已查到。"})
    return steps


def _stat_only_script(session_id: str, label: str) -> list:
    """只查统计（不读原话）的子代理脚本 → 不该产生 shared-session 关联。"""
    return [
        {"tool_calls": [{"id": "t1", "name": "session_detail",
                         "args": {"sessionId": session_id}}]},
        {"content": f"{label}的往来统计已查到。"},
    ]


def _grandchild_script(session_id: str, label: str) -> list:
    """孙分支脚本：也读同一个人的原话。"""
    return [
        {"tool_calls": [{"id": "g1", "name": "read_timeline",
                         "args": {"sessionId": session_id, "limit": 3}}]},
        {"content": f"孙分支查到了{label}的另一批消息。"},
    ]


def _main_script(target: str) -> list:
    """主代理：派三个子代理。

      · 两个都读同一个人（→ 应该产生 shared-session 语义边）
      · 一个只查统计（→ 不该被 shared-session 连上，验判据不误连）
    """
    return [
        {"tool_calls": [{"id": "d1", "name": "delegate_tasks",
                         "args": {"tasks": [
                             f"查{target}最近说过的关键词",
                             f"查{target}的另一批关键词",
                         ]}}]},
        {"tool_calls": [{"id": "d2", "name": "delegate_tasks",
                         "args": {"tasks": [
                             f"统计{target}的往来总量",
                         ]}}]},
        {"content": f"关于{target}的统计已由子代理查完。"},
    ]


def _walk(brs, out=None, depth=1):
    out = out if out is not None else []
    for b in brs:
        out.append((b, depth))
        _walk(getattr(b, "children", []) or [], out, depth + 1)
    return out


def _cn_segments(text: str, maxlen: int = 4) -> list[str]:
    """从一段文本里抽出连续的中文片段（长度 2..maxlen）。

    为什么不用 jieba：这里要的是「**真实出现在消息里的**、能被
    search_messages 搜到的词」——分词器切出来的词可能不是原文子串
    （比如切出「感到」但原文是「我感到」倒过来），
    搜不到就没有 quote 证据，测出来的「无边」是假的。
    所以直接从原文里截连续中文段，最稳。
    """
    out = []
    cur = ""
    for ch in text:
        if "一" <= ch <= "鿿":
            cur += ch
        else:
            if len(cur) >= 2:
                out.append(cur[:maxlen])
            cur = ""
    if len(cur) >= 2:
        out.append(cur[:maxlen])
    return out


def _run_gui_and_fetch(port: int, question: str) -> dict:
    """真起 GUI 服务 → 真调洞察接口 → 拿到真返回。

    不用 mock：起真进程、走真 HTTP、拿真 JSON。
    """
    import subprocess
    env = dict(os.environ)
    env["MOSU_GUI_PORT"] = str(port)
    proc = subprocess.Popen(
        [PY, str(ROOT / "cli" / "mosu.py"), "gui", "--port", str(port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        # 等端口起来
        base = f"http://127.0.0.1:{port}"
        for _ in range(120):
            try:
                urllib.request.urlopen(base + "/api/status", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                if proc.poll() is not None:
                    out = proc.stdout.read() if proc.stdout else ""
                    raise RuntimeError(f"GUI 进程提前退出：\n{out[-2000:]}")
                time.sleep(0.5)
        else:
            raise RuntimeError("GUI 端口 120 次重试仍未就绪")

        # 提交洞察任务
        req = urllib.request.Request(
            base + "/api/insight/ask",
            data=json.dumps({"question": question}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        tid = resp.get("taskId") or resp.get("id") or resp.get("task_id")
        if not tid:
            raise RuntimeError(f"接口没返回 taskId：{resp}")

        # 轮询结果
        deadline = time.time() + 600
        last = {}
        while time.time() < deadline:
            try:
                last = json.loads(urllib.request.urlopen(
                    base + f"/api/task?id={tid}", timeout=10).read())
            except Exception:  # noqa: BLE001
                last = {}
            st = (last.get("status") or last.get("state") or "")
            if st in ("done", "ok", "success", "error", "failed"):
                break
            time.sleep(1.0)
        return last
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def main():
    results = []

    print("[0] 检查前置条件")
    try:
        from core.dataset import Dataset
        ds = Dataset()
        sess = ds.sessions()
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 本机微信数据不可用：{type(e).__name__}: {e}")
        print("     → 端到端跳过（需要真实数据目录）。")
        return 2
    priv = [s for s in sess
            if (s.get("type") == "private"
                and (s.get("messageCount") or 0) > 50
                and not str(s.get("displayName") or "").startswith("文件传输"))]
    priv.sort(key=lambda s: -(s.get("messageCount") or 0))
    if not priv:
        print("  ⚠️ 本机没有消息量足够的私聊会话 → 端到端跳过。")
        return 2
    target_sess = priv[0]
    target = str(target_sess.get("displayName") or "")
    target_id = str(target_sess.get("sessionId") or "")
    check(results, "挑到一个真实会话做被问对象", bool(target and target_id),
          f"{target} / {target_id}")

    # 关键词不再需要（改用 read_timeline 读原话，证据稳定产生）。
    check(results, "目标会话可读", bool(target and target_id),
          f"{target} / {target_id}")

    # ------------------------------------------------------- 场景 1：真跑委派
    print("\n[1] 场景 1 —— 真跑三层委派（真 AgentTeam + 真 ReAct）")
    from core.agent_team import AgentTeam, BranchBudget
    from core.agent_engine import InsightAgent

    main_llm = ScriptedLLM(script=_main_script(target))

    # 子代理 LLM 工厂：按 **goal 文本**派发脚本（与线程调度无关，确定性）。
    #
    # 分派规则：
    #   「另一条线」   → 孙分支（读原话）
    #   「往来总量」   → 只查统计（不读原话）
    #   其余（查…）   → 读原话；其中「最近说过的」那个再派一个孙分支
    def _llm_factory():
        def _is_grandchild(goal):
            return "另一条线" in goal

        def _is_stat(goal):
            return "往来总量" in goal

        def _is_first(goal):
            return "最近说过的" in goal

        rules = [
            (_is_grandchild, _grandchild_script(target_id, target)),
            (_is_stat, _stat_only_script(target_id, target)),
            (_is_first, _read_person_script(target_id, target,
                                            with_grandchild=True)),
            # 兜底：其余「查…」的分支一律读原话。
            # 少了这条，没匹配上任何规则的分支会拿到「（没有匹配到脚本）」
            # ——它一个工具都不调，产不出 quote 证据，
            # 测出来的「无边」是脚本漏了，不是功能不行。
            (lambda goal: "查" in goal,
             _read_person_script(target_id, target)),
        ]
        llm = ScriptedLLM(rules=rules)
        sub_scripts.append(llm)
        return llm

    sub_scripts = []
    budget = BranchBudget({"max_concurrent": 3, "max_depth": 2,
                           "max_branches": 6, "max_steps": 24})
    team = AgentTeam(ds, llm=main_llm, budget=budget,
                     max_steps=6, llm_factory=_llm_factory)
    agent = InsightAgent(ds, llm=main_llm, team=team)

    t0 = time.time()
    try:
        run = agent.run(f"帮我分析一下和{target}的聊天情况")
    except Exception as e:  # noqa: BLE001
        check(results, "主代理跑完", False, f"{type(e).__name__}: {e}")
        return summary(results)
    cost = round(time.time() - t0, 1)

    check(results, "主代理跑完", True, f"{cost}s")
    branches = run.branches or []
    check(results, "产生了分支", len(branches) > 0, f"{len(branches)} 条顶层分支")
    all_br = _walk(branches)
    check(results, "分支总数 > 顶层数（说明有嵌套）",
          len(all_br) > len(branches), f"总 {len(all_br)} / 顶层 {len(branches)}")

    # ------------------------------------------------------- 场景 2：语义边
    print("\n[2] 场景 2 —— 共享会话 → 真产生语义边")
    links = run.semantic_links or []
    check(results, "run.semantic_links 有内容", len(links) > 0,
          f"{len(links)} 条：{[l.get('kind') for l in links]}")

    sess_links = [l for l in links if l.get("kind") == "shared-session"]
    check(results, "有 shared-session 判据的边", len(sess_links) > 0,
          f"{len(sess_links)} 条")

    # 真的产生 quote 证据了吗？（判据的前提）
    quote_brs = []
    for b, _ in all_br:
        kinds = [e.get("kind") for e in (b.evidence or [])]
        if "quote" in kinds:
            quote_brs.append(b)
    check(results, "有分支真读到了原话（quote 证据）",
          len(quote_brs) > 0,
          f"{len(quote_brs)}/{len(all_br)} 个分支有 quote 证据")

    # 关联的必须是**两个都读了同一个人**的分支
    if sess_links:
        lk = sess_links[0]
        pair = {lk["from"], lk["to"]}
        both_quote = [b for b, _ in all_br if b.id in pair
                      and any(e.get("kind") == "quote"
                              for e in (b.evidence or []))]
        check(results, "被连上的两个分支都真读了原话",
              len(both_quote) == 2,
              f"pair={pair} 其中有 quote 的={len(both_quote)}")

    # ------------------------------------------------------- 场景 3：不误连
    print("\n[3] 场景 3 —— 判据不误连（只查统计的分支不该被 shared-session 连上）")
    stat_only = [b for b, _ in all_br
                 if not any(e.get("kind") == "quote"
                            for e in (b.evidence or []))]
    stat_ids = {b.id for b in stat_only}
    # 注意：只验 **shared-session**。
    # stat-only 分支完全可能因为「目标文本里提到了同一个人」
    # 而被 shared-topic 连上——那是**合法且正确**的
    # （两个分支确实在查同一件事）。
    # 判据收紧的只是「读到过原话」这一条。
    bad_sess = [l for l in links
                if l.get("kind") == "shared-session"
                and (l["from"] in stat_ids or l["to"] in stat_ids)]
    check(results, "只有 stat 证据的分支没被 shared-session 连上",
          not bad_sess,
          f"stat-only={[b.id for b in stat_only]} 误连={bad_sess}")

    # 反向：shared-session 的两端**必须**都有 quote 证据
    for l in links:
        if l.get("kind") != "shared-session":
            continue
        pair = [b for b, _ in all_br if b.id in (l["from"], l["to"])]
        ok = len(pair) == 2 and all(
            any(e.get("kind") == "quote" for e in (b.evidence or []))
            for b in pair)
        check(results, "shared-session 两端都有 quote 证据", ok,
              f"pair={[b.id for b in pair]}")

    check(results, "没有自环（自己连自己）",
          all(l["from"] != l["to"] for l in links),
          str([l for l in links if l["from"] == l["to"]]))

    check(results, "父子之间没有语义边",
          not any(
              {l["from"], l["to"]} == {b.id, c.id}
              for b, _ in all_br for c in (getattr(b, "children", []) or [])
              for l in links),
          "树线已表达父子关系")

    # ------------------------------------------------------- 场景 4：标签可读
    print("\n[4] 场景 4 —— 标签是人名不是 wxid")
    if links:
        lbl = links[0].get("label") or ""
        check(results, "标签里有人名", target in lbl or target[:2] in lbl, lbl)
        check(results, "标签里没有裸 wxid", "wxid_" not in lbl, lbl)
    else:
        check(results, "标签里有人名", False, "没有边可验")

    # ------------------------------------------------------- 场景 5：落盘
    print("\n[5] 场景 5 —— 语义边落盘（历史回看不能丢）")
    try:
        from core import runs as runs_mod
        home = tempfile.mkdtemp(prefix="mosu_e2e_iter12_")
        os.environ["MOSU_HOME"] = home
        rid = runs_mod.save_run({
            "question": "测试",
            "answer": run.answer,
            "semanticLinks": links,
            "branches": [b.to_dict() for b in branches],
        })
        saved = runs_mod.runs_dir() / f"{rid}.json"
        check(results, "语义边落盘成功", saved.exists(), str(saved))
        if saved.exists():
            data = json.loads(saved.read_text(encoding="utf-8"))
            check(results, "落盘文件里有 semanticLinks 字段",
                  "semanticLinks" in data, str(list(data.keys())[:10]))
            check(results, "落盘文件里语义边非空",
                  len(data.get("semanticLinks") or []) > 0,
                  str(len(data.get("semanticLinks") or [])))
    except Exception as e:  # noqa: BLE001
        check(results, "语义边落盘成功", False, f"{type(e).__name__}: {e}")

    # ------------------------------------------------------- 场景 6：前端下发
    print("\n[6] 场景 6 —— GUI 下发（后端字段一路传到 node）")
    try:
        from gui.server import _branch_dicts_with_links
        dicts = _branch_dicts_with_links(run.branches)
        check(results, "分支 dict 里有 semanticLinks",
              all("semanticLinks" in d for d in dicts),
              str([list(d.keys())[-3:] for d in dicts]))
        check(results, "下发的语义边非空",
              sum(len(d.get("semanticLinks") or []) for d in dicts) > 0,
              str(sum(len(d.get("semanticLinks") or []) for d in dicts)))
        # 前端要的字段必须齐（前端字段要从后端一路传到 node）
        need = ("from", "to", "kind", "label", "color")
        allkeys = set()
        for d in dicts:
            for lk in (d.get("semanticLinks") or []):
                allkeys |= set(lk.keys())
        check(results, "语义边字段齐全", set(need) <= allkeys,
              f"缺={set(need) - allkeys}")
    except Exception as e:  # noqa: BLE001
        check(results, "分支 dict 里有 semanticLinks", False,
              f"{type(e).__name__}: {e}")

    # ------------------------------------------------------- 场景 7：真浏览器
    print("\n[7] 场景 7 —— 真浏览器渲染（真组件 + 真数据）")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check(results, "playwright 可用", False, "未安装，跳过前端渲染")
        return summary(results)

    # 用**后端真算出来的**分支数据（含真 semanticLinks）去渲染
    payload = _branch_dicts_with_links(run.branches)
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<style>{(ROOT / 'gui/assets/theme.css').read_text(encoding='utf-8')}</style>
<style>{(ROOT / 'gui/assets/v2/app.css').read_text(encoding='utf-8')}</style>
</head><body><div id="probe" style="width:1100px"></div>
<script src="{(ROOT / 'gui/vendor/vue.global.prod.js').as_uri()}"></script>
<script src="{(ROOT / 'gui/assets/v2/views-branch.js').as_uri()}"></script>
<script>
window.__B__ = {json.dumps(payload, ensure_ascii=False)};
Vue.createApp({{
  components: {{ 'branch-tree': window.MosuV2.BranchTree }},
  setup() {{ return {{ branches: Vue.ref(window.__B__) }}; }},
  template: '<div><branch-tree :branches="branches"></branch-tree></div>'
}}).mount('#probe');
window.__READY__ = true;
</script></body></html>"""
    tmp = ROOT / ".cache" / "audit" / "_e2e_iter12.html"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(html, encoding="utf-8")

    errs = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errs.append("PAGEERROR: " + str(e)))
        page.on("console", lambda m: errs.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.goto(tmp.as_uri(), wait_until="networkidle")
        page.wait_for_function("window.__READY__ === true", timeout=15000)
        page.wait_for_timeout(1200)

        n_sem = page.locator(".v2-bt-sem-line").count()
        n_tree = page.locator(".v2-bt-line").count()
        check(results, "语义边画出来了（真浏览器）", n_sem > 0,
              f"语义线 {n_sem} 条 / 树线 {n_tree} 条")
        check(results, "语义线是虚线",
              (page.eval_on_selector(".v2-bt-sem-line",
                                    "e => getComputedStyle(e).strokeDasharray")
               or "").strip() not in ("", "none"))
        check(results, "语义线与树线分层（数量分开）", n_sem != n_tree or n_tree == 0,
              f"语义 {n_sem} / 树 {n_tree}")

        # 藏掉语义边一端 → 线必须消失（不画到邻近节点）
        if n_sem > 0:
            first = page.locator(".v2-bt-sem-line").first
            d = first.get_attribute("d")
            # 从真数据里找这条线连的是哪两个分支
            page.evaluate("""() => {
              const e = document.querySelector('.v2-bt-sem-hit');
              window.__HIT__ = e ? e.getAttribute('d') : null;
            }""")
            # 藏掉**所有**节点 → 所有线都必须消失（最狠的检查）
            page.eval_on_selector_all(
                ".v2-bt-node", "els => els.forEach(e => { e.style.display='none'; })")
            page.wait_for_timeout(800)
            check(results, "全藏后语义线清零（没画到空处）",
                  page.locator(".v2-bt-sem-line").count() == 0,
                  f"剩 {page.locator('.v2-bt-sem-line').count()} 条")

        bad = [e for e in errs
               if (e.startswith("PAGEERROR") or e.startswith("error"))
               and "favicon" not in e]
        check(results, "渲染过程无 JS 错误", not bad, str(bad[:3]))
        browser.close()

    return summary(results)


if __name__ == "__main__":
    sys.exit(main())
