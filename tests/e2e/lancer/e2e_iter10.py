"""迭代 10 端到端：证据账本升级（对标 Lancer OrchestrationEvidenceRecord）。

验证八件事：
  1. 真起 GUI → 洞察能跑通，答案有内容
  2. **每条证据都带 source**（db / message / agent / model 四选一）
  3. **每条数字证据都带口径 scope**（统计对象 / 时间范围 / 统计维度）
  4. **quote 证据 source 是 message 且 status 是 verified**
     （原话能点开核对 = 可核验；推断类才是 reported）
  5. **契约里 no_stat_without_scope 不被违反**（口径这条约束真的生效）
  6. **inferences 字段存在**（模型的判断跟查出来的事实分开摆）
  7. **前端真的渲染口径行**（v2 前端模板里能看到 scopeLine 的调用）
  8. **工具层直调也带三件套**（绕开模型，直接调 ToolBox）

为什么必须真跑
--------------
「source/status/scope 三个字段有没有」这件事，单元测试能造出来；
但「真跑一轮洞察，模型调了工具之后产出的证据有没有口径」，
只有真跑才知道——模型可能根本不调工具，或者调了别的工具。

另外「前端有没有渲染」必须真取一次 HTML 页面，
不能只 grep 源文件就算数（那个源文件可能根本没被加载）。

前置条件
--------
本机 8787 端口要有可用的 OpenAI 兼容网关（WorkBuddy Console）。
没有网关时脚本会明确报告跳过，而不是伪装通过。

用法：python tests/e2e/lancer/e2e_iter10.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# 仓库根：向上找带 core/ 的那层，不硬编码目录深度
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "core").is_dir() and (p / "cli").is_dir())
sys.path.insert(0, str(ROOT))

GUI_PORT = 8801
BASE = f"http://127.0.0.1:{GUI_PORT}"
PY = sys.executable
ENV_FILE = ROOT / ".env"

VALID_SOURCES = {"db", "message", "agent", "model"}
VALID_STATUS = {"verified", "reported"}


def _load_env() -> dict:
    out = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def check(results, name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def http(path, payload=None, timeout=600):
    url = BASE + path
    if payload is None:
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode("utf-8", "ignore")}
    except Exception as e:      # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def gateway_ready(cfg: dict) -> bool:
    base = (cfg.get("MOSU_BOT_BASE_URL") or "").rstrip("/")
    key = cfg.get("MOSU_BOT_API_KEY") or ""
    if not base or not key:
        return False
    try:
        req = urllib.request.Request(
            base + "/models", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.status == 200
    except Exception:
        return False


def _pick_contact() -> str:
    """从本机真实数据里挑一个私聊对象（消息量最多的那个）。

    为什么不能写死人名：模型查不到会直接答「没找到」，
    证据池空 → 后面所有场景都失去意义。
    端到端必须用真实数据才有意义。
    """
    try:
        from core.dataset import Dataset
        sess = Dataset().sessions()
    except Exception:
        return ""
    priv = [s for s in sess
            if (s.get("type") == "private"
                and (s.get("messageCount") or 0) > 50
                and not str(s.get("displayName") or "").startswith("文件传输")
                and "AI" not in str(s.get("displayName") or ""))]
    if not priv:
        priv = [s for s in sess if (s.get("messageCount") or 0) > 50]
    if not priv:
        return ""
    priv.sort(key=lambda s: -(s.get("messageCount") or 0))
    return str(priv[0].get("displayName") or "")


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


def main():
    results = []
    env_cfg = _load_env()

    print("[0] 检查前置条件")
    if not gateway_ready(env_cfg):
        print("  ⚠️ 本机 8787 网关不可用")
        print("     → 本轮端到端跳过。真机验证需要可用的 OpenAI 兼容网关。")
        print("     字段层面的校验已由 tests/test_evidence_ledger.py（32 例）覆盖。")
        return 2

    home = tempfile.mkdtemp(prefix="mosu_e2e_iter10_")
    env = dict(os.environ)
    env["MOSU_HOME"] = home
    env["MOSU_BOT_BASE_URL"] = env_cfg["MOSU_BOT_BASE_URL"]
    env["MOSU_BOT_API_KEY"] = env_cfg["MOSU_BOT_API_KEY"]
    env["MOSU_BOT_MODEL"] = env_cfg.get("MOSU_BOT_MODEL") or "deepseek-v4.1-flash"
    env["MOSU_NO_DOTENV"] = "1"

    print("  独立配置目录:", home)
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
        "llm": {
            "baseUrl": env["MOSU_BOT_BASE_URL"],
            "apiKey": env["MOSU_BOT_API_KEY"],
            "model": env["MOSU_BOT_MODEL"],
        },
    }, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.Popen(
        [PY, str(ROOT / "cli" / "mosu.py"), "gui", "--port", str(GUI_PORT),
         "--no-browser"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        up = False
        for _ in range(60):
            time.sleep(0.5)
            try:
                urllib.request.urlopen(BASE + "/api/status", timeout=3).read()
                up = True
                break
            except Exception:
                continue
        if not check(results, "GUI 服务启动", up):
            return summary(results)

        # ---------------------------------------------------------- 场景 A
        print("\n[1] 场景 A —— 真跑一轮洞察")
        # 问一个必然要调工具、且会产生数字证据的问题。
        # 「统计一下我和张三的聊天总量」→ find_contact + chat_stats(ranking)
        # 问一个**本机真实存在**的人。写死「张三」的话，
        # 模型会答「没找到叫张三的人」→ 证据池空 → 后面场景全废。
        # （端到端第一次跑就栽在这：结论不是 bug，是测试数据不真实。）
        target = _pick_contact()
        check(results, "挑到一个真实会话做被问对象", bool(target),
              target or "本机没有私聊会话")
        if not target:
            return summary(results)

        def ask_run(question: str, wait_s: int = 240) -> dict:
            """问一轮洞察，等它跑完，拿回完整结果。

            /api/insight/ask 是**异步**的：立即返回 task id，结果在任务里。
            （拿到返回就直接读 answer 会得到空——这是异步契约，不是 bug。）
            """
            ask = http("/api/insight/ask", {"question": question})
            tid = (ask.get("id") or ask.get("taskId") or "")
            out = {"_taskId": tid}
            for _ in range(wait_s):      # 最多等 4 分钟
                time.sleep(1.0)
                if not tid:
                    break
                t = http(f"/api/task?id={tid}")
                st = t.get("status")
                if st in ("done", "error"):
                    out = t.get("result") or {}
                    # _taskId 必须单独存：out 会被 result 整个覆盖掉，
                    # 混在一起写就会丢（第一版就是这么丢的）。
                    out["_taskId"] = tid
                    out["_status"] = st
                    out["_error"] = t.get("error") or ""
                    break
            return out

        # 第一轮：统计型问题 → 产出 stat 证据（source=db）
        res = ask_run(f"统计一下我和{target}的聊天总量，我发出多少条？")
        check(results, "ask 返回任务 id", bool(res.get("_taskId")),
              str(res.get("_taskId") or ""))
        check(results, "任务跑完", res.get("_status") == "done",
              f"status={res.get('_status')} err={res.get('_error') or ''} "
              f"ans={(res.get('answer') or '')[:50]}")
        answer = (res.get("answer") or "")
        evidence = res.get("evidence") or []
        steps = res.get("steps") or []
        check(results, "洞察跑出答案", bool(answer.strip()), answer[:70])
        check(results, "答案非空且不是错误串",
              bool(answer.strip()) and "失败" not in answer[:20],
              answer[:70])
        check(results, "模型确实调了工具",
              any(s.get("tool") for s in steps),
              f"steps={len(steps)}")
        check(results, "产出了证据", len(evidence) > 0, f"{len(evidence)} 条")

        # 第二轮：要原话 + 要态度 → 逼模型走检索路径，产出 quote 证据
        # （source=message）并可能给出主观判断（推断标注）。
        # 为什么必须单独跑一轮：第一轮的问题只要统计，模型不会去检索，
        # quote 分支和推断分支就永远走不到——断言会变成「软通过」，
        # 看着绿，其实没验到东西。
        res2 = ask_run(
            f"搜一下我和{target}的聊天记录里关于「工作」的对话，"
            f"列出几条他的原话；再说你觉得他最近对我是什么态度。")
        check(results, "第二轮跑完", res2.get("_status") == "done",
              f"status={res2.get('_status')} err={res2.get('_error') or ''}")
        answer2 = (res2.get("answer") or "")
        evidence2 = res2.get("evidence") or []
        check(results, "第二轮也产出了证据", len(evidence2) > 0,
              f"{len(evidence2)} 条")
        check(results, "第二轮模型确实调了工具",
              any(s.get("tool") for s in (res2.get("steps") or [])),
              f"steps={len(res2.get('steps') or [])}")
        quote2 = [e for e in evidence2 if e.get("kind") == "quote"]
        check(results, "第二轮产出了 quote（原话）证据", len(quote2) > 0,
              f"{len(quote2)} 条")

        # ---------------------------------------------------------- 场景 B
        print("\n[2] 场景 B —— 每条证据都带 source")
        # 两轮合并看：第一轮统计（db）+ 第二轮检索原话（message）
        all_ev = list(evidence) + list(evidence2)
        srcs = [e.get("source") for e in all_ev]
        check(results, "没有证据缺 source",
              all(s in VALID_SOURCES for s in srcs),
              f"取值={sorted(set(srcs))}")
        # 断言必须是「真的两种以上」——原来写的是 >=1，
        # 看着绿其实什么都没验到（第一轮只调统计工具，只可能出 db）。
        check(results, "source 至少覆盖两种来源（db + message）",
              len(set(srcs)) >= 2, f"{sorted(set(srcs))}")

        # ---------------------------------------------------------- 场景 C
        print("\n[3] 场景 C —— 数字证据带口径")
        stats = [e for e in evidence if e.get("kind") == "stat"]
        quotes = [e for e in evidence if e.get("kind") == "quote"]
        check(results, "有数字证据", len(stats) > 0, f"{len(stats)} 条")

        def _scope_ok(e):
            sc = e.get("scope")
            if isinstance(sc, dict):
                return any(str(sc.get(k) or "").strip()
                           for k in ("scope", "period", "metric"))
            if isinstance(sc, str):
                return bool(sc.strip())
            return False

        bare = [e for e in stats if not _scope_ok(e)]
        check(results, "所有数字证据都带口径", not bare,
              f"缺口径 {len(bare)} 条" if bare else f"{len(stats)} 条全带")
        if stats:
            sample = stats[0].get("scope")
            txt = json.dumps(sample, ensure_ascii=False) if sample else ""
            check(results, "口径内容非空且含时间或维度",
                  bool(txt) and len(txt) > 4, txt[:110])

        # ---------------------------------------------------------- 场景 D
        print("\n[4] 场景 D —— quote 证据可核验")
        quotes = [e for e in all_ev if e.get("kind") == "quote"]
        check(results, "quote 证据存在", len(quotes) > 0, f"{len(quotes)} 条")
        if quotes:
            check(results, "quote 证据 source 是 message",
                  all(e.get("source") == "message" for e in quotes),
                  str(sorted({e.get("source") for e in quotes})))
            check(results, "quote 证据 status 是 verified",
                  all(e.get("status") == "verified" for e in quotes),
                  str(sorted({e.get("status") for e in quotes})))
            check(results, "quote 证据带原文全文（可点开核对）",
                  all(e.get("full") for e in quotes),
                  f"{sum(1 for e in quotes if e.get('full'))}/{len(quotes)} 条")
            check(results, "quote 证据带定位信息（会话/时间/发送者）",
                  all(e.get("sessionId") or e.get("time") for e in quotes),
                  f"{sum(1 for e in quotes if e.get('sessionId') or e.get('time'))}"
                  f"/{len(quotes)} 条")

        # ---------------------------------------------------------- 场景 E
        print("\n[5] 场景 E —— 契约里口径约束生效")
        contract = res.get("contract") or {}
        forb = {f.get("id"): f for f in (contract.get("forbidden") or [])}
        check(results, "契约含 no_stat_without_scope",
              "no_stat_without_scope" in forb,
              str(sorted(forb.keys())))
        if "no_stat_without_scope" in forb:
            check(results, "no_stat_without_scope 未被违反",
                  not forb["no_stat_without_scope"].get("violated"),
                  str(forb["no_stat_without_scope"].get("detail") or ""))

        # ---------------------------------------------------------- 场景 F
        print("\n[6] 场景 F —— 推断标注独立存在")
        infs = res.get("inferences")
        check(results, "响应里有 inferences 字段", infs is not None,
              f"type={type(infs).__name__}")
        check(results, "inferences 是列表", isinstance(infs, list),
              str(type(infs).__name__))
        if isinstance(infs, list):
            check(results, "每条推断都是字符串",
                  all(isinstance(x, str) for x in infs),
                  str(infs[:3]))
            # 推断句不该含数字（有数字 = 有可核验载体）
            bad = [x for x in infs if any(c.isdigit() for c in x)]
            check(results, "推断句不含数字（数字意味着有出处）",
                  not bad, str(bad[:2]))
            # 推断句必须真的是答案里的原句（不能是凭空造的标签）
            inans = [x for x in infs
                     if x not in (res.get("answer") or "")
                     and x.rstrip("…") not in (res.get("answer") or "")]
            check(results, "推断句都来自答案原文", not inans, str(inans[:2]))

        # 第二轮：模型被明确问「你觉得他什么态度」→ 应该给出主观判断
        infs2 = res2.get("inferences")
        check(results, "第二轮响应里有 inferences 字段", infs2 is not None,
              f"type={type(infs2).__name__}")
        check(results, "第二轮 inferences 是列表", isinstance(infs2, list),
              str(type(infs2).__name__))
        if isinstance(infs2, list):
            check(results, "第二轮推断句都是字符串",
                  all(isinstance(x, str) for x in infs2), str(infs2[:3]))
            bad2 = [x for x in infs2 if any(c.isdigit() for c in x)]
            check(results, "第二轮推断句不含数字", not bad2, str(bad2[:2]))
        # 打印第二轮答案原文：人工核对推断标注用
        print("  · 第二轮答案原文（供人工核对推断标注）：")
        for ln in (answer2 or "").split("\n"):
            if ln.strip():
                print("    | " + ln[:160])

        # **硬断言**：第二轮明确问了「你觉得他什么态度」，
        # 模型必然给出主观判断 → inferences 必须非空。
        # 之前这里是「字段存在就算过」，等于没验——
        # 第一版词表漏检导致整轮 inferences 为空，端到端照样全绿。
        # 这条断言就是为了把那类「看着有、实际不工作」的问题挡在门外。
        check(results, "第二轮推断标注非空（模型确实下了判断）",
              len(infs2 or []) > 0,
              f"{len(infs2 or [])} 条：" + str((infs2 or [])[:2]))
        # 答案里如果模型自报了「我的判断 / 结论」，那必须被标出来——
        # 这是最强的推断信号，漏了就说明判据又窄了。
        if "我的判断" in answer2 or "我的结论" in answer2:
            check(results, "模型自报的「我的判断」被标出来了",
                  any("判断" in x or "结论" in x for x in (infs2 or [])),
                  str((infs2 or [])[:2]))
        # 标出来的每一句都必须是答案里的原句
        if infs2:
            notin = [x for x in infs2
                     if x not in answer2 and x.rstrip("…") not in answer2]
            check(results, "第二轮推断句都来自答案原文", not notin,
                  str(notin[:2]))

        # ---------------------------------------------------------- 场景 G
        print("\n[7] 场景 G —— 前端真的渲染口径与推断")
        try:
            with urllib.request.urlopen(BASE + "/", timeout=10) as r:
                page = r.read().decode("utf-8", "ignore")
            check(results, "首页能打开", bool(page), f"{len(page)} 字节")
        except Exception as e:      # noqa: BLE001
            check(results, "首页能打开", False, str(e))
            page = ""
        # 前端是 v2 的模块化结构，模板在 /assets/v2/views-insight.js
        try:
            with urllib.request.urlopen(
                    BASE + "/assets/v2/views-insight.js", timeout=10) as r:
                v2 = r.read().decode("utf-8", "ignore")
            check(results, "v2 洞察脚本能取到", bool(v2), f"{len(v2)} 字节")
            check(results, "前端调用了 scopeLine（渲染口径行）",
                  "scopeLine(e)" in v2, "")
            check(results, "前端渲染了推断区",
                  "inferences" in v2 and "v2-ia-ev-infer" in v2, "")
            check(results, "前端展示了转述标记",
                  "transcribed" in v2, "")
        except Exception as e:      # noqa: BLE001
            check(results, "v2 洞察脚本能取到", False, str(e))

        # ---------------------------------------------------------- 场景 H
        print("\n[8] 场景 H —— 工具层直调也带三件套（绕开模型）")
        try:
            from core.dataset import Dataset
            from core.agent_tools import ToolBox
            ds = Dataset()
            box = ToolBox(ds)
            r1 = box.run("chat_stats", {"metric": "ranking", "top": 5})
            ev1 = r1.get("evidence") or []
            check(results, "直调 chat_stats 产出证据", len(ev1) > 0,
                  f"{len(ev1)} 条")
            check(results, "直调证据全部带 source",
                  all(e.get("source") in VALID_SOURCES for e in ev1),
                  str(sorted({e.get("source") for e in ev1})))
            check(results, "直调证据全部带 status",
                  all(e.get("status") in VALID_STATUS for e in ev1),
                  str(sorted({e.get("status") for e in ev1})))
            check(results, "直调数字证据全部带口径",
                  all(_scope_ok(e) for e in ev1),
                  f"{sum(1 for e in ev1 if _scope_ok(e))}/{len(ev1)}")
            # 换时间范围，口径要跟着变
            r2 = box.run("chat_stats", {"metric": "ranking", "top": 3,
                                        "from": "2025-01-01", "to": "2025-06-30"})
            ev2 = r2.get("evidence") or []
            p1 = json.dumps(ev1[0].get("scope"), ensure_ascii=False) if ev1 else ""
            p2 = json.dumps(ev2[0].get("scope"), ensure_ascii=False) if ev2 else ""
            check(results, "时间范围变化时口径跟着变", p1 != p2,
                  f"{p1[:60]} vs {p2[:60]}")
            if ev2:
                sc2 = ev2[0].get("scope") or {}
                check(results, "口径里带上了具体时间段",
                      "2025" in str(sc2.get("period") or ""),
                      str(sc2.get("period") or ""))
        except Exception as e:      # noqa: BLE001
            check(results, "直调 chat_stats 产出证据", False,
                  f"{type(e).__name__}: {e}")

        # ---------------------------------------------------------- 场景 I
        print("\n[9] 场景 I —— 落盘里也有 inferences（历史回看）")
        run_id = res.get("runId")
        if run_id:
            hist = http(f"/api/runs/detail?id={run_id}")
            got = hist.get("inferences")
            check(results, "落盘记录里有 inferences", got is not None,
                  f"type={type(got).__name__}")
            check(results, "落盘的 inferences 与响应一致",
                  (got or []) == (infs or []),
                  f"{len(got or [])} vs {len(infs or [])}")
            # 落盘里也要能重建出证据的来源与口径（历史回看不能丢信息）
            ev_saved = hist.get("evidence") or []
            check(results, "落盘的证据带 source",
                  all(e.get("source") in VALID_SOURCES for e in ev_saved),
                  str(sorted({e.get("source") for e in ev_saved})))
            check(results, "落盘的数字证据带口径",
                  all(_scope_ok(e) for e in ev_saved
                      if e.get("kind") == "stat"),
                  f"{sum(1 for e in ev_saved if e.get('kind') == 'stat')} 条")
        else:
            check(results, "落盘记录里有 inferences", False, "没拿到 runId")

        # 第二轮也要落盘（第二轮才有推断句，是检验落盘完整性的关键样本）
        run_id2 = res2.get("runId")
        if run_id2:
            hist2 = http(f"/api/runs/detail?id={run_id2}")
            got2 = hist2.get("inferences")
            check(results, "第二轮落盘里有 inferences", got2 is not None,
                  f"type={type(got2).__name__}")
            check(results, "第二轮落盘的 inferences 与响应一致",
                  (got2 or []) == (infs2 or []),
                  f"{len(got2 or [])} vs {len(infs2 or [])}")
            ev2_saved = hist2.get("evidence") or []
            check(results, "第二轮落盘的 quote 证据带 source=message",
                  all(e.get("source") == "message" for e in ev2_saved
                      if e.get("kind") == "quote"),
                  str(sorted({e.get("source") for e in ev2_saved})))
            check(results, "第二轮落盘的 quote 证据带全文",
                  all(e.get("full") for e in ev2_saved
                      if e.get("kind") == "quote"),
                  f"{sum(1 for e in ev2_saved if e.get('kind') == 'quote')} 条")
        else:
            check(results, "第二轮落盘里有 inferences", False, "没拿到 runId")

        return summary(results)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
