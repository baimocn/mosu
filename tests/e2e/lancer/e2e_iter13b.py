"""迭代 13 · B 组端到端：补迭代 7 报告里「未跑」的项。

迭代 7 的报告记了 9 条 not_run，其中 4 条是**功能缺口**：
  契约判定失败时前端不白屏 / 历史回看界面渲染阶段契约 /
  forbidden 第 4 条在真实语料上的召回率 / 多代理分支场景下的阶段归类。

本脚本逐条真跑：
  B1 契约判定失败时前端不白屏 —— 真构造 pipeline.evaluate 抛异常，
     断言：①整轮不失败（答案照样给）②界面明确提示「验收环节未生成」
     ③页面不白屏（有答案正文、有错误提示区）
  B2 历史回看界面渲染阶段契约 —— 落盘的 record.contract 真被渲染出来
  B3 forbidden 第 4 条在**真实语料**上的召回率 —— 用真库真工具调用造样本，
     量化「有引号但没 quote 证据」这条的触发率与误报面
  B4 多代理分支场景下的阶段归类 —— 真的 delegate_tasks 之后，
     看阶段判定会不会把子代理的调用算进主轨迹

用法：python tests/e2e/lancer/e2e_iter13b.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "core").is_dir() and (p / "cli").is_dir())
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8805"
PY = sys.executable

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def http(path, payload=None, method=None, timeout=60):
    url = BASE + path
    if payload is None:
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method or "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}",
                "body": e.read().decode("utf-8", "ignore")}


def main():
    from playwright.sync_api import sync_playwright

    home = tempfile.mkdtemp(prefix="mosu_e2e_iter13b_")
    env = dict(os.environ)
    env["MOSU_HOME"] = home
    env["MOSU_NO_DOTENV"] = "1"
    for k in ("MOSU_BOT_BASE_URL", "MOSU_BOT_API_KEY", "MOSU_BOT_MODEL"):
        env.pop(k, None)

    # **本脚本进程自己也要指向同一个临时目录**。
    #
    # 第一版只在 B3 段才设 os.environ["MOSU_HOME"]，
    # 结果 B2 段的 runs.save_run 写到了**用户真实的 ~/.mosu/runs/**
    # ——测试数据污染用户目录，还导致 GUI 服务读不到那条记录。
    # 落盘类操作必须在任何 core import 之前就把 HOME 指过去。
    os.environ["MOSU_HOME"] = home
    os.environ["MOSU_NO_DOTENV"] = "1"
    for k in ("MOSU_BOT_BASE_URL", "MOSU_BOT_API_KEY", "MOSU_BOT_MODEL"):
        os.environ.pop(k, None)

    print("[0] 准备独立配置目录:", home)
    real_vault = Path.home() / ".mosu" / "config.json"
    accounts = {}
    if real_vault.exists():
        try:
            accounts = json.loads(real_vault.read_text(encoding="utf-8")).get(
                "accounts") or {}
        except Exception:
            accounts = {}
    vault = Path(home) / "config.json"
    vault.write_text(json.dumps({
        "accounts": accounts,
        "current": (list(accounts.keys())[0] if accounts else ""),
        "llm": {"baseUrl": "http://127.0.0.1:1/v1", "apiKey": "sk-none",
                "model": "no-model"},
    }, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.Popen(
        [PY, str(ROOT / "cli" / "mosu.py"), "gui", "--port", "8805", "--no-browser"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        up = False
        for _ in range(40):
            time.sleep(0.5)
            try:
                urllib.request.urlopen(BASE + "/api/status", timeout=3).read()
                up = True
                break
            except Exception:
                continue
        if not check("GUI 服务启动", up):
            return summary()
        print()

        # =============================================== B1 契约判定失败不白屏
        print("[B1] 契约判定失败 —— 前端不白屏，且明确提示")
        # 真构造：把 pipeline.evaluate 打坏，看整轮会怎样。
        # 怎么打坏而不改产品代码？在 GUI 服务进程里做不到——
        # 所以走「代理层」：起一个包装服务？太重。
        #
        # 换一条更真的路：**直接单测层面真跑一遍失败分支**，
        # 再用**真浏览器**验前端拿到空契约时的界面表现。
        # 两段合起来才等于「契约判定失败时用户看到什么」。
        from core.agent_engine import AgentRun, _evaluate_contract
        from core import pipeline

        # B1.1 真跑产品的兜底代码：
        # 把 pipeline.evaluate 打坏 → 调 agent_engine._evaluate_contract
        # （**产品那份函数**，不是复刻品——原来这段 try/except 埋在
        #   AgenticRunner.run() 里，外部根本触达不到，
        #   想测只能复刻一遍逻辑，等于没测产品代码。
        #   迭代 13 把它抽成模块级函数，这里才能真打坏、真验。）
        real_eval = pipeline.evaluate
        try:
            def _boom(run):
                raise RuntimeError("迭代13 故意制造的契约判定异常")
            pipeline.evaluate = _boom
            ar = AgentRun(question="契约炸了")
            ar.answer = "这是答案"
            ar.contract = _evaluate_contract(ar)
            check("B1.1 契约判定抛异常时兜底成带 error 的结构（不静默）",
                  "error" in (ar.contract or {})
                  and "stages" in (ar.contract or {}),
                  str(ar.contract)[:120])
            check("B1.2 兜底结构里 stages 为空（前端据此判断不渲染契约带）",
                  (ar.contract or {}).get("stages") == [])
            check("B1.3 兜底结构 okToProceed=False（不谎称通过）",
                  (ar.contract or {}).get("okToProceed") is False)
            check("B1.3b 兜底结构带上了异常信息（可诊断）",
                  "RuntimeError" in str((ar.contract or {}).get("error", "")),
                  str((ar.contract or {}).get("error", "")))
            # 恢复后应正常工作（兜底不是常态）
            pipeline.evaluate = real_eval
            ar2 = AgentRun(question="正常")
            ar2.answer = "有证据的答案"
            ar2.steps = [{"tool": "chat_stats", "ok": True}]
            ar2.evidence = [{"kind": "stat", "scope": {"scope": "x",
                                                        "period": "y",
                                                        "metric": "z"}}]
            ar2.contract = _evaluate_contract(ar2)
            check("B1.3c 判定正常时产出真契约（有 stages，不带 error）",
                  bool((ar2.contract or {}).get("stages"))
                  and "error" not in (ar2.contract or {}),
                  f"stages={len((ar2.contract or {}).get('stages') or [])}")
        finally:
            pipeline.evaluate = real_eval

        # B1.4 真浏览器：接口返回空契约时，界面不白屏
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            pg = b.new_page(viewport={"width": 1440, "height": 1100})
            errs = []
            pg.on("pageerror", lambda e: errs.append("PAGEERROR: " + str(e)))
            pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}")
                  if m.type == "error" else None)
            # 拦截接口，把 contract 换成空 —— 模拟判定失败。
            #
            # 三个都要拦：
            #  1. /api/insight/ask  → 返回 taskId
            #  2. /api/insight/stream → **SSE**，不拦的话它会自己连上，
            #     前端就走 SSE 路径了，mock 的 /api/task 根本用不上
            #  3. /api/task → 轮询保底路径。返回结构必须是
            #     {"status":"done", "result": {...}}——前端读的是 t.result，
            #     第一版把字段摊在顶层，前端拿到空 result，答案自然是空的。
            pg.route("**/api/insight/ask**", lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"taskId": "fake-task"})))
            pg.route("**/api/insight/stream**", lambda route: route.fulfill(
                status=200, content_type="text/event-stream",
                body="event: error\ndata: {}\n\n"))
            pg.route("**/api/task**", lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({
                    "status": "done",
                    "result": {
                        "answer": "这是答案正文",
                        "steps": [], "evidence": [], "inferences": [],
                        "contract": {}, "branches": [], "settlement": {},
                        "cost": 1, "model": "test-model",
                    },
                })))
            pg.goto(BASE + "/#/insight", wait_until="networkidle")
            pg.wait_for_timeout(2000)
            pg.fill(".v2-ia-input", "契约判定失败会怎样")
            pg.click(".v2-ia-go")
            pg.wait_for_timeout(2500)

            body_len = len(pg.content())
            check("B1.4 契约为空时页面没白屏（DOM 有内容）",
                  body_len > 2000, f"{body_len} 字符")
            check("B1.5 答案正文照样显示（主链路没被契约拖垮）",
                  "这是答案正文" in pg.content(),
                  "答案在页面里" if "这是答案正文" in pg.content() else "答案不在")
            check("B1.6 界面明确提示「验收环节未生成」",
                  pg.locator(".v2-ia-contract-missing").count() == 1,
                  f"{pg.locator('.v2-ia-contract-missing').count()} 个提示")
            check("B1.7 契约带没渲染（空契约不该画空壳）",
                  pg.locator(".v2-ia-contract").count() == 0,
                  f"{pg.locator('.v2-ia-contract').count()} 个")
            check("B1.8 提示文案里点明「未生成」",
                  "未生成" in (pg.locator(".v2-ia-contract-missing").first
                               .inner_text() if pg.locator(
                                   ".v2-ia-contract-missing").count() else ""),
                  pg.locator(".v2-ia-contract-missing").first.inner_text()
                  if pg.locator(".v2-ia-contract-missing").count() else "(无提示)")
            check("B1.9 无 JS 错误", not errs, str(errs[:3]))
            b.close()
        print()

        # ====================================== B2 历史回看渲染阶段契约
        print("[B2] 历史回看 —— 落盘的契约真被渲染出来")
        # 先在落盘目录里造一条带 contract 的历史记录（走真的 save_run）
        from core import runs as runs_mod
        fake_contract = {
            "stages": [
                {"id": "scope", "label": "范围认定", "actor": "local",
                 "goal": "确认这次问的是谁", "required": False,
                 "status": {"code": "OK", "label": "通过", "okToProceed": True,
                            "hint": "", "detail": ""},
                 "summary": "stage=scope status=OK calls=1"},
                {"id": "probe", "label": "数据探测", "actor": "local",
                 "goal": "确认数据能查到", "required": True,
                 "status": {"code": "EMPTY", "label": "未通过", "okToProceed": False,
                            "hint": "查了但一条数据都没拿到", "detail": ""},
                 "summary": "stage=probe status=EMPTY calls=1 evidence=0"},
            ],
            "gates": [
                {"stage": "scope", "statusCode": "OK", "okToProceed": True,
                 "summary": "stage=scope status=OK calls=1", "facts": {}},
                {"stage": "probe", "statusCode": "EMPTY", "okToProceed": False,
                 "summary": "stage=probe status=EMPTY calls=1 evidence=0", "facts": {}},
            ],
            "forbidden": [
                {"id": "no_stat_as_quote", "text": "统计口径不能当某人说过的话",
                 "why": "混淆两者就是撒谎", "violated": True,
                 "detail": "只有统计口径、没有原话"},
            ],
            "okToProceed": False,
        }
        rid = runs_mod.save_run({
            "id": "hist_contract_probe",
            "question": "历史回看契约渲染探针",
            "answer": "这是历史答案",
            "steps": [], "evidence": [], "inferences": [],
            "branches": [], "semanticLinks": [], "settlement": {},
            "contract": fake_contract,
            "modelRouting": {},
            "tokens": 0, "cost": 1, "model": "test-model",
            "createdAt": time.time(), "status": "done",
        })
        # 落盘目录跟 GUI 服务进程的 MOSU_HOME 一致吗？GUI 是子进程，
        # 它用的是同一份 env（MOSU_HOME=home），所以一致。
        check("B2.1 带 contract 的历史记录落盘成功", bool(rid), rid)

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            pg = b.new_page(viewport={"width": 1440, "height": 1100})
            errs = []
            pg.on("pageerror", lambda e: errs.append("PAGEERROR: " + str(e)))
            pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}")
                  if m.type == "error" else None)
            pg.goto(BASE + "/#/insight", wait_until="networkidle")
            pg.wait_for_timeout(1800)
            # 展开历史面板
            pg.click(".v2-ia-hist-btn")
            pg.wait_for_timeout(1500)
            check("B2.2 历史面板列出了刚落的记录",
                  "历史回看契约渲染探针" in pg.content(),
                  "列表里有" if "历史回看契约渲染探针" in pg.content()
                  else "列表里没有")
            # 点开它
            pg.click(".v2-ia-hist-item:has-text('历史回看契约渲染探针')")
            pg.wait_for_timeout(1500)
            check("B2.3 历史详情区渲染了契约带（迭代 7 记的 not_run）",
                  pg.locator(".v2-ia-hist-detail .v2-ia-contract").count() == 1,
                  f"{pg.locator('.v2-ia-hist-detail .v2-ia-contract').count()} 个")
            stages = pg.eval_on_selector_all(
                ".v2-ia-hist-detail .v2-ia-cstage",
                "els => els.map(e => [e.getAttribute('data-stage-id'),"
                " e.getAttribute('data-status-code')])")
            check("B2.4 契约带里阶段行与落盘一致",
                  stages == [["scope", "OK"], ["probe", "EMPTY"]], str(stages))
            check("B2.5 未通过的阶段带 bad 标记（不只靠颜色）",
                  pg.locator(".v2-ia-hist-detail .v2-ia-cstage.bad").count() == 1,
                  f"{pg.locator('.v2-ia-hist-detail .v2-ia-cstage.bad').count()} 个")
            check("B2.6 违反的禁止项也渲染出来了",
                  "统计口径不能当某人说过的话" in pg.content(),
                  "有" if "统计口径不能当某人说过的话" in pg.content() else "没有")
            check("B2.7 标题是「当时的验收环节」",
                  "当时的验收环节" in pg.content(),
                  "有" if "当时的验收环节" in pg.content() else "没有")
            check("B2.8 无 JS 错误", not errs, str(errs[:3]))
            b.close()
        print()

        # ============================ B3 forbidden 第 4 条在真实语料上的召回率
        print("[B3] forbidden 第 4 条（引用具体话语但没读过原文）"
              "—— 真实语料上的表现")
        # 真库真工具：跑几个真实工具调用，收集真实证据样本
        os.environ["MOSU_HOME"] = home
        from core.dataset import Dataset
        from core.agent_tools import ToolBox
        ds = Dataset()
        tb = ToolBox(ds)
        sessions = ds.sessions()
        check("B3.0 真实库可用（会话数 > 0）", len(sessions) > 0,
              f"{len(sessions)} 个会话")

        # 取几个真实会话，跑真实的 read_timeline，收集真实 quote 证据
        real_quote_samples = []
        real_stat_samples = []
        for s in sessions[:6]:
            # 会话 id 的字段名是 **sessionId**（core/dataset.py sessions() 的返回结构）。
            #
            # 第一版这里取的是 `s.get("username") or s.get("id")` → 全是 None →
            # sid 为空 → `_resolve` 返回 None → 工具返回空证据 → 收集到 0 条。
            # 症状是「真实语料召回率 0%」，看起来像门禁太严，
            # 其实是测试自己没拿到数据。**空数据不能拿来判召回率**。
            sid = s.get("sessionId") or ""
            if not sid:
                continue
            r = tb.run("read_timeline", {"sessionId": sid, "limit": 8})
            for ev in (r.get("evidence") or []):
                if ev.get("kind") == "quote":
                    real_quote_samples.append(ev)
            r2 = tb.run("chat_stats", {"sessionId": sid})
            for ev in (r2.get("evidence") or []):
                if ev.get("kind") == "stat":
                    real_stat_samples.append(ev)
        check("B3.1 从真实语料收集到 quote 证据",
              len(real_quote_samples) > 0, f"{len(real_quote_samples)} 条")
        check("B3.2 从真实语料收集到 stat 证据",
              len(real_stat_samples) > 0, f"{len(real_stat_samples)} 条")

        # 构造 4 类样本，看第 4 条的判定
        # 样本 A：有 quote 证据 + 答案带引号 → 应**不**违规（真读过原文）
        # 样本 B：无 quote 证据 + 答案带引号 → 应**违规**（真违规）
        # 样本 C：无 quote 证据 + 答案不带引号 → 应**不**违规
        # 样本 D：有 quote 证据 + 答案带引号但引号内容不在证据里
        #        → 当前实现**不**报（盲区），这就是要量化的漏报面
        q_ev = real_quote_samples[0] if real_quote_samples else {"kind": "quote"}
        s_ev = real_stat_samples[0] if real_stat_samples else {"kind": "stat"}
        cases = [
            ("A 读过原文 + 答案引用", [q_ev],
             "他说过「这个方案不行」", False),
            ("B 没读原文 + 答案引用", [s_ev],
             "他说过「这个方案不行」", True),
            ("C 没读原文 + 答案不引用", [s_ev],
             "这个月消息量是 1200 条", False),
            ("D 读过原文但引号内容不在证据里", [q_ev],
             "他说过「完全另一句话」", False),
        ]
        from core.pipeline import evaluate
        verdicts = []
        for label, evs, ans, expect_violated in cases:
            c = evaluate({
                "steps": [{"tool": "read_timeline", "ok": True}],
                "evidence": evs, "answer": ans,
                "toolNumbers": set(), "branches": [],
                "unsourcedNumbers": [],
            })
            f4 = next((f for f in c["forbidden"]
                       if f["id"] == "no_quote_without_read"), None)
            got = bool(f4 and f4.get("violated"))
            verdicts.append((label, got, expect_violated))
            check(f"B3.3 {label} → 判定为"
                  f"{'违规' if got else '不违规'}（期望{'违规' if expect_violated else '不违规'}）",
                  got == expect_violated,
                  f"detail={f4.get('detail') if f4 else 'N/A'}")
        # 量化：在真实语料上，有 quote 证据的样本中第 4 条永不触发
        #      → 说明它的**召回**完全依赖「有没有 quote 证据」这一个信号，
        #      对「引号内容是否真的被读过」零分辨力。
        check("B3.4 盲区量化：读过原文但引号内容是编的 → 不报错（已知漏报）",
              verdicts[3][1] is False,
              "第 4 条只判「有没有引号」+「有没有 quote 证据」，"
              "不校验引号内容是否真在证据里")
        # 真实语料上的触发率：拿真实 quote 证据 + 各种答案形态统计
        triggered = 0
        total = 0
        for evs in ([q_ev], [s_ev], []):
            for ans in ("他说过「x」", "依据：消息量 100", "普通叙述没有引用"):
                c = evaluate({
                    "steps": [{"tool": "read_timeline", "ok": True}],
                    "evidence": evs, "answer": ans,
                    "toolNumbers": set(), "branches": [],
                    "unsourcedNumbers": [],
                })
                f4 = next((f for f in c["forbidden"]
                           if f["id"] == "no_quote_without_read"), None)
                total += 1
                if f4 and f4.get("violated"):
                    triggered += 1
        check("B3.5 真实证据组合下的触发率已量化",
              total == 9, f"{triggered}/{total} 触发")

        # 真跑一次：真实 quote 证据 + 带引号答案 → 不误报（这是关键：不能误伤）
        c_real = evaluate({
            "steps": [{"tool": "read_timeline", "ok": True}],
            "evidence": real_quote_samples[:3],
            "answer": "根据他说的「最近有点忙」",
            "toolNumbers": set(), "branches": [], "unsourcedNumbers": [],
        })
        f4_real = next((f for f in c_real["forbidden"]
                        if f["id"] == "no_quote_without_read"), None)
        check("B3.6 真实 quote 证据 + 带引号答案 → 不误报（关键）",
              not (f4_real and f4_real.get("violated")),
              str(f4_real))
        print()

        # ========================= B4 多代理分支场景下的阶段归类
        print("[B4] 多代理分支（delegate_tasks）场景下的阶段归类")
        # 真问题（迭代 13 实测）：多代理委派时，主代理只调 delegate_tasks
        # （归到 orchestrate 阶段），数据是**子代理**查的。
        # 原来的阶段判定只看主代理的工具调用 →
        #   probe 判 MISSING（未执行）、read 判 SKIPPED（未涉及）、
        #   okToProceed=False → **一次成功的多代理回答被判成「没通过」**。
        #
        # 现在判据改成「调用过 **或** 有对应证据」。
        c_multi = evaluate({
            "steps": [{"tool": "delegate_tasks", "ok": True}],
            "evidence": [
                {"kind": "stat", "scope": {"scope": "x", "period": "y",
                                           "metric": "z"}},
                {"kind": "quote", "sessionId": "s1", "localId": 1,
                 "sender": "张三", "text": "在吗"},
            ],
            "answer": "他说过「在吗」",
            "toolNumbers": set(), "branches": [], "unsourcedNumbers": [],
        })
        stages = {s["id"]: s["status"]["code"] for s in c_multi["stages"]}
        check("B4.1 多代理成功：probe 不再误判 MISSING",
              stages.get("probe") == "OK", str(stages))
        check("B4.2 多代理成功：read 不再误判 SKIPPED",
              stages.get("read") == "OK", str(stages))
        check("B4.3 多代理成功：整体 okToProceed=True",
              c_multi["okToProceed"] is True, str(c_multi["okToProceed"]))
        probe_sum = next((s["summary"] for s in c_multi["stages"]
                          if s["id"] == "probe"), "")
        check("B4.4 机械摘要标出 delegated=True（可追溯是委派来的证据）",
              "delegated=True" in probe_sum, probe_sum)
        # **反向**：委派了但什么都没查到 → 门禁不能放松
        c_none = evaluate({
            "steps": [{"tool": "delegate_tasks", "ok": True}],
            "evidence": [], "answer": "我不知道",
            "toolNumbers": set(), "branches": [], "unsourcedNumbers": [],
        })
        stages_none = {s["id"]: s["status"]["code"]
                       for s in c_none["stages"]}
        check("B4.5 反向：委派了但没查到 → probe 仍判 MISSING（门禁没放松）",
              stages_none.get("probe") == "MISSING", str(stages_none))
        check("B4.6 反向：没查到就不能放行",
              c_none["okToProceed"] is False, str(c_none["okToProceed"]))
        # 单代理场景不受影响（回归）
        c_single = evaluate({
            "steps": [{"tool": "chat_stats", "ok": True}],
            "evidence": [{"kind": "stat",
                          "scope": {"scope": "x", "period": "y", "metric": "z"}}],
            "answer": "消息量 100 条", "toolNumbers": {"100"},
            "branches": [], "unsourcedNumbers": [],
        })
        st_single = {s["id"]: s["status"]["code"]
                     for s in c_single["stages"]}
        check("B4.7 单代理场景判定不变（probe 仍 OK）",
              st_single.get("probe") == "OK", str(st_single))
        # 纯 orchestrate、无证据、无调用 → probe MISSING（老行为）
        c_bare = evaluate({
            "steps": [], "evidence": [], "answer": "",
            "toolNumbers": set(), "branches": [], "unsourcedNumbers": [],
        })
        st_bare = {s["id"]: s["status"]["code"] for s in c_bare["stages"]}
        check("B4.8 什么都没做 → probe MISSING（老行为不变）",
              st_bare.get("probe") == "MISSING", str(st_bare))
        print()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)

    return summary()


def summary():
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*56}")
    print(f"迭代 13 B 组（迭代 7 未跑项）：{passed}/{total} 通过")
    failed = [n for n, ok in results if not ok]
    if failed:
        print("失败项：")
        for n in failed:
            print("  ❌", n)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
