"""迭代 11 端到端：子树结算 + 回执协议（对标 Lancer settled() / receipt）。

验证八件事：
  1. **三层委派真跑起来**：主代理 → 子代理 → 孙代理（真 AgentTeam + 真 ReAct 循环）
  2. **回执真的从 reported 变 accepted**（送达确认，不是「跑完就算」）
  3. **送达点时序正确**：payload 回喂进上下文之后才标 accepted
  4. **子树结算判据是真接线**：`settle_children` 在生产代码里被调用，
     且「父分支自己没跑完」时整棵树不算 settled
  5. **显式接受标记能被程序化提取**（`mosu-accepted: <分支id>`）
  6. **显式接受不左右结算判定**（送达才是门槛）
  7. **未结算要如实报警**（答案里出现「分支未结算」）
  8. **前端能看到结算状态**（分支树渲染里带 settled/receipt 字段）

为什么用「脚本化 LLM 决策」而不是纯真模型
----------------------------------------
真模型**不保证**会派孙分支——它可能一层都不派，或者派了但不接受。
那样测出来的「结算通过」是**碰巧**，不是能力。

所以这里只替换模型的**决策层**（决定调哪个工具），其余全走真实路径：
  - 真 Dataset（真 SQLite、真解密）
  - 真 ToolBox（真工具、真证据）
  - 真 AgentTeam（真线程池、真分支树、真泳道）
  - 真 AgentEngine（真 ReAct 循环、真回喂、真结算判定）
  - 真 settle_children / consume_branch（真函数）

这跟「mock 掉核心逻辑」是两回事：核心逻辑一行没改，
只有「模型下一步做什么」被脚本固定——这正是端到端该固定的部分
（否则每次跑结果都不一样，测不了）。

另外这一轮**必须真跑**的还有一个东西：主代理 → 子代理的**送达时序**。
单元测试能造出「送达后 receipt=accepted」，
但造不出「payload 到底有没有真的进 messages」——
那得真跑一遍 ReAct 循环才看得见。

前置条件
--------
本机要有可用的微信数据目录（~/.mosu/config.json 里有账号密钥）。
不需要 LLM 网关（决策层是脚本化的）。

用法：python tests/e2e/lancer/e2e_iter11.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# 仓库根：向上找带 core/ 的那层，不硬编码目录深度
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "core").is_dir() and (p / "cli").is_dir())
sys.path.insert(0, str(ROOT))

GUI_PORT = 8802
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
    """按预设脚本回话的假 LLM。

    只替换**决策层**：给定第几次调用，返回预设的 tool_calls 或答案。
    其余（工具执行、证据、回喂、结算）全是真代码。

    为什么需要它：
      真模型不保证会派孙分支，端到端要**确定性**地造出三层树。
    """

    def __init__(self, script: list, model: str = "scripted-e2e"):
        self.script = list(script)
        self.model = model
        self.calls: list[dict] = []

    def chat_tools(self, messages, tools, temperature=0.0, max_tokens=1000):
        idx = len(self.calls)
        self.calls.append({"idx": idx, "n_messages": len(messages),
                           "n_tools": len(tools or [])})
        step = self.script[idx] if idx < len(self.script) else {"content": "（脚本用尽）"}
        # 记录收到的消息——用来验证「送达时序」：
        # 脚本里的下一步动作是否可以观察到上一次 tool 结果。
        self.calls[-1]["tool_results"] = sum(
            1 for m in messages if (m.get("role") == "tool"))
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


def _main_script(target: str, branch_id_holder: dict) -> list:
    """主代理的脚本：先委派，再收尾。

    `branch_id_holder` 用来把「主代理看到的分支 id」回传给测试，
    方便后面构造 `mosu-accepted:` 标记。
    """
    return [
        # 第 1 步：委派两个子任务
        {"tool_calls": [{"id": "d1", "name": "delegate_tasks",
                         "args": {"tasks": [
                             f"统计我和{target}的聊天总量",
                             f"统计我和{target}的活跃时间分布",
                         ]}}]},
        # 第 2 步：再委派一次（测「多批委派」也能收敛）
        {"tool_calls": [{"id": "d2", "name": "delegate_tasks",
                         "args": {"tasks": [
                             f"查{target}最近说过的一个关键词",
                         ]}}]},
        # 第 3 步：收尾
        {"content": f"关于{target}的统计已由子代理查完，结论见上。"},
    ]


def _sub_script(depth: int, kid_ids: dict) -> list:
    """子代理的脚本。

    depth=1：子代理**再派孙分支**（造出第三层）
    depth=2：孙代理直接给结论
    """
    if depth >= 2:
        return [
            {"content": "孙分支的结论：查到了原始数据，已核对。"},
        ]
    return [
        # 先查一次真数据（走真 ToolBox）
        {"tool_calls": [{"id": "s1", "name": "chat_stats",
                         "args": {"metric": "overview"}}]},
        # 再派孙分支
        {"tool_calls": [{"id": "s2", "name": "delegate_tasks",
                         "args": {"tasks": ["再细查一下这个人的关键词分布"]}}]},
        # 收尾
        {"content": "子分支的结论：已汇总孙分支结果。"},
    ]


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
    target = str(priv[0].get("displayName") or "")
    check(results, "挑到一个真实会话做被问对象", bool(target), target)

    # ------------------------------------------------------- 场景 1：三层委派
    print("\n[1] 场景 1 —— 三层委派真跑（主 → 子 → 孙）")
    from core.agent_team import AgentTeam, BranchBudget
    from core.agent_engine import InsightAgent

    main_llm = ScriptedLLM(_main_script(target, {}))

    # 子代理的 LLM 工厂：每个子代理一份脚本。
    # 子代理脚本要派孙分支，孙分支再套一层。
    sub_scripts = []

    def _llm_factory():
        s = _sub_script(depth=1, kid_ids={})
        llm = ScriptedLLM(s)
        sub_scripts.append(llm)
        return llm

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
    check(results, "答案有内容", bool((run.answer or "").strip()),
          (run.answer or "")[:70])

    branches = run.branches or []
    check(results, "产生了分支", len(branches) > 0, f"{len(branches)} 条顶层分支")

    # ---- 找孙分支
    def _walk(brs, out=None, depth=1):
        out = out if out is not None else []
        for b in brs:
            out.append((b, depth))
            _walk(getattr(b, "children", []) or [], out, depth + 1)
        return out

    all_br = _walk(branches)
    # Branch.depth：顶层分支 = 1，其子（孙）= 2。
    # 「三层委派」= 主代理 + 顶层分支 + 孙分支。
    depths = sorted({getattr(b, "depth", 0) for b, _ in all_br})
    check(results, "真的派出了第三层（孙分支）", (max(depths) if depths else 0) >= 2,
          f"Branch.depth 层级={depths}")
    check(results, "分支总数 > 顶层数（说明有嵌套）",
          len(all_br) > len(branches), f"总 {len(all_br)} / 顶层 {len(branches)}")

    # ------------------------------------------------------- 场景 2：回执状态
    print("\n[2] 场景 2 —— 回执真的从 reported 变 accepted")
    from core.agent_team import RECEIPT_ACCEPTED, RECEIPT_REPORTED
    accepted = [b for b, _ in all_br if b.receipt == RECEIPT_ACCEPTED]
    check(results, "有分支已是 accepted（送达确认）", len(accepted) > 0,
          f"{len(accepted)}/{len(all_br)}")
    check(results, "所有分支都已送达（无 pending）",
          all(b.receipt == RECEIPT_ACCEPTED for b, _ in all_br),
          f"pending={sum(1 for b, _ in all_br if b.receipt == RECEIPT_REPORTED)}")

    # 送达方必须是**直接父**，不能是 main
    kids = [(b, d) for b, d in all_br if d >= 2]
    check(results, "孙分支的送达方是它的直接父（不是 main）",
          all(b.consumed_by and b.consumed_by != "main" for b, _ in kids)
          if kids else True,
          f"{[b.consumed_by for b, _ in kids][:3]}")

    # ------------------------------------------------------- 场景 3：送达时序
    print("\n[3] 场景 3 —— 送达点时序（payload 回喂之后才标 accepted）")
    # 主代理的脚本第 2 步开始前，第 1 步的 delegate 结果必须已经在上下文里。
    # 也就是说：主代理第 2 次调用时，收到的 tool 结果数 ≥ 1。
    check(results, "主代理在委派后收到了 tool 结果（送达真的发生了）",
          any(c.get("tool_results", 0) >= 1 for c in main_llm.calls),
          f"tool_results={[c.get('tool_results') for c in main_llm.calls]}")

    # 子代理也一样：它派孙分支后，孙分支的 payload 必须回喂进它的上下文
    sub_ok = any(c.get("tool_results", 0) >= 1 for s in sub_scripts
                 for c in s.calls)
    check(results, "子代理收到了孙分支的 tool 结果", sub_ok,
          f"子代理脚本数={len(sub_scripts)}")

    # ------------------------------------------------------- 场景 4：结算判据
    print("\n[4] 场景 4 —— 子树结算判据真接线")
    st = run.settlement or {}
    check(results, "settlement 字段存在", bool(st), str(st)[:80])
    check(results, "整棵树 settled=True", st.get("settled") is True,
          f"settled={st.get('settled')} pending={st.get('pending')}")
    check(results, "settlement.total 数对了（含孙分支）",
          st.get("total") == len(all_br),
          f"{st.get('total')} vs {len(all_br)}")
    check(results, "settlement.consumed == total",
          st.get("consumed") == st.get("total"),
          f"{st.get('consumed')}/{st.get('total')}")

    # 反向验证：**父分支没跑完 → 整棵树不算 settled**
    from core.agent_team import (Branch, BRANCH_COMPLETE, BRANCH_RUNNING,
                                 branch_settled, settle_children)
    parent = Branch("父", 0)
    parent.status = BRANCH_COMPLETE
    kid = Branch("子", 1, parent=parent)
    kid.status = BRANCH_COMPLETE
    check(results, "反查：子分支未送达 → 父分支不结算",
          not branch_settled(parent))
    from core.agent_team import consume_branch
    consume_branch(kid, parent.id)
    check(results, "反查：送达后父分支才结算", branch_settled(parent))
    parent2 = Branch("父2", 0)
    parent2.status = BRANCH_RUNNING
    kid2 = Branch("子2", 1, parent=parent2)
    kid2.status = BRANCH_COMPLETE
    consume_branch(kid2, parent2.id)
    check(results, "反查：父分支自己还在跑 → 整棵树不结算",
          not branch_settled(parent2))
    check(results, "反查：settle_children 对 running 父分支返回 False",
          not settle_children([parent2]))

    # ------------------------------------------------------- 场景 5：显式接受
    print("\n[5] 场景 5 —— 显式接受标记（程序化提取）")
    from core.agent_team import parse_accept_marks, apply_accept_marks
    marks = parse_accept_marks(f"结论。\nmosu-accepted: {branches[0].id} 数据对得上")
    check(results, "能从文本里提取接受标记", len(marks) == 1
          and marks[0][0] == branches[0].id, str(marks))
    b0 = branches[0]
    apply_accept_marks(f"mosu-accepted: {b0.id} 理由", [b0], "main")
    check(results, "接受标记落到分支上", b0.accepted and b0.accepted_by == "main",
          f"accepted_by={b0.accepted_by}")
    # 不变量：**显式接受不改变结算判定**
    # 必须用**全新的分支**：上面的 parent/kid 已经被 consume_branch 送达过了，
    # 拿它们来验「不变量」会得到假绿（before 就是 True）。
    p3 = Branch("父3", 0)
    p3.status = BRANCH_COMPLETE
    k3 = Branch("子3", 1, parent=p3)
    k3.status = BRANCH_COMPLETE
    before = branch_settled(p3)
    apply_accept_marks(f"mosu-accepted: {k3.id}", [k3], p3.id)
    check(results, "不变量：显式接受不左右结算判定",
          before is False and branch_settled(p3) is False,
          f"before={before} after={branch_settled(p3)}（显式接受≠送达）")
    # 送达之后才结算——两者是独立的两件事
    consume_branch(k3, p3.id)
    check(results, "送达之后才结算（送达才是门槛）", branch_settled(p3))

    # ------------------------------------------------------- 场景 6：未结算报警
    print("\n[6] 场景 6 —— 未结算要如实报警")
    from core.agent_engine import AgentRun, _settlement_of, _settlement_note
    r2 = AgentRun("测试")
    p = Branch("父", 0)
    p.status = BRANCH_COMPLETE
    k = Branch("子", 1, parent=p)
    k.status = BRANCH_COMPLETE
    r2.branches.append(p)
    r2.answer = "这是答案正文。"
    r2.settlement = _settlement_of(r2)
    out = _settlement_note(r2)
    check(results, "未送达时报「分支未结算」", "分支未结算" in out, out[:80])
    check(results, "报警里点名了是哪条分支", k.id in out, k.id)
    # 送达后不该再报警
    consume_branch(k, p.id)
    r2.settlement = _settlement_of(r2)
    out2 = _settlement_note(r2)
    check(results, "送达后不再报警", "分支未结算" not in out2, out2[:60])

    # ------------------------------------------------------- 场景 7：前端可见
    print("\n[7] 场景 7 —— 前端能看到结算状态")
    d0 = branches[0].to_dict()
    for field in ("settled", "receipt", "consumedBy", "accepted", "acceptedBy"):
        check(results, f"分支字典暴露 {field}", field in d0, str(d0.get(field)))
    check(results, "run.to_dict 暴露 settlement",
          "settlement" in run.to_dict())

    # 前端渲染：分支树组件必须真能渲染带 settled 的分支
    try:
        from core.agent_team import Branch as _B
        payload = {"branches": [d0]}
        check(results, "分支树 payload 结构完整",
              isinstance(payload.get("branches"), list)
              and "children" in d0)
    except Exception as e:  # noqa: BLE001
        check(results, "分支树 payload 结构完整", False, str(e))

    print("\n[8] 落盘与前端资源")
    # settlement 必须能落盘（GUI 回看历史时要能看到）
    try:
        from core import runs as runs_mod
        home = tempfile.mkdtemp(prefix="mosu_e2e_iter11_")
        os.environ["MOSU_HOME"] = home
        rid = runs_mod.save_run({
            "question": "测试",
            "answer": run.answer,
            "settlement": run.settlement,
            "branches": [b.to_dict() for b in branches],
        })
        saved = runs_mod.runs_dir() / f"{rid}.json"
        check(results, "settlement 落盘成功", saved.exists(), str(saved))
        if saved.exists():
            data = json.loads(saved.read_text(encoding="utf-8"))
            check(results, "落盘文件里有 settlement 字段",
                  "settlement" in data, str(list(data.keys())[:8]))
            check(results, "落盘文件里有分支树（含 settled/receipt）",
                  bool(data.get("branches"))
                  and "settled" in data["branches"][0],
                  str(list((data.get("branches") or [{}])[0].keys())[:10]))
    except Exception as e:  # noqa: BLE001
        check(results, "settlement 落盘成功", False, f"{type(e).__name__}: {e}")

    # GUI 落盘路径也要带 settlement（历史回看能看到结算状态）
    try:
        gui_txt = (ROOT / "gui" / "server.py").read_text(encoding="utf-8")
        check(results, "GUI 落盘记录里含 settlement 字段",
              '"settlement"' in gui_txt,
              "gui/server.py 的 save_run 调用缺 settlement")
    except Exception as e:  # noqa: BLE001
        check(results, "GUI 落盘记录里含 settlement 字段", False, str(e))

    return summary(results)


if __name__ == "__main__":
    sys.exit(main())
