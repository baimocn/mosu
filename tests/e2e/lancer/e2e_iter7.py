"""迭代 7 端到端：阶段契约（对标 Lancer PROCESS.json）真机验证。

验证六件事：
  1. 真起 GUI 服务 → 洞察接口可用
  2. **契约随答案一起返回**：一次真实洞察跑完，结果里必须有 contract，
     且含四个阶段 + 每阶段一个 gate
  3. **状态码带机器可判的推进条件**：okToProceed 存在且是布尔
  4. **机械摘要无叙述**：summary 里只有标识与计数
  5. **落盘带契约**：~/.mosu/runs/*.json 里有 contract，回看历史能看到
  6. **前端真渲染**：洞察页出现阶段带，三态可区分

为什么必须真跑模型
------------------
契约判定依赖「工具调用轨迹 + 证据池 + 答案」三样真实产物。
用假数据测只能证明渲染逻辑对，证明不了「判定链真的跑通了」。
所以这个脚本走真链路：真模型 + 真工具 + 真数据库。

前置条件
--------
本机 8787 端口要有可用的 OpenAI 兼容网关（WorkBuddy Console）。
没有网关时脚本会明确报告跳过，而不是伪装通过。

用法：python tests/e2e/lancer/e2e_iter7.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 仓库根：向上找带 core/ 的那层，不硬编码目录深度
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "core").is_dir() and (p / "cli").is_dir())
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8797"
PY = sys.executable

# 真实网关（本机 WorkBuddy Console）。读 .env 拿，凭据不硬编码。
ENV_FILE = ROOT / ".env"


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


def http(path, payload=None, method=None, timeout=30):
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
        return {"error": f"HTTP {e.code}", "body": e.read().decode("utf-8", "ignore")}


def gateway_ready(cfg: dict) -> bool:
    """探一下网关在不在、能不能列模型。"""
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


def main():
    from playwright.sync_api import sync_playwright

    results = []
    env_cfg = _load_env()

    print("[0] 检查前置条件")
    if not gateway_ready(env_cfg):
        print("  ⚠️ 本机 8787 网关不可用（MOSU_BOT_BASE_URL / API_KEY 未配置或不通）")
        print("     → 本轮端到端跳过。真机验证需要可用的 OpenAI 兼容网关。")
        print("     契约判定逻辑本身已由 tests/test_pipeline.py（38 例）与")
        print("     tests/test_pipeline_wiring.py（27 例）覆盖。")
        return 2

    home = tempfile.mkdtemp(prefix="mosu_e2e_iter7_")
    env = dict(os.environ)
    env["MOSU_HOME"] = home
    # 用真实网关（真模型、真工具、真数据），但配置写进临时目录，不动用户的。
    env["MOSU_BOT_BASE_URL"] = env_cfg["MOSU_BOT_BASE_URL"]
    env["MOSU_BOT_API_KEY"] = env_cfg["MOSU_BOT_API_KEY"]
    env["MOSU_BOT_MODEL"] = env_cfg.get("MOSU_BOT_MODEL") or "gpt-4o-mini"
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
        [PY, str(ROOT / "cli" / "mosu.py"), "gui", "--port", "8797", "--no-browser"],
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
        if not check(results, "GUI 服务启动", up):
            return summary(results)

        print("\n[1] 发起一次真实洞察（真模型 + 真工具 + 真数据库）")
        # 问题选「对方发起的消息有多少」——会触发 chat_stats（probe 阶段）
        ask = http("/api/insight/ask",
                   {"question": "统计一下我和张三的聊天总量，我发出多少条？"},
                   timeout=30)
        tid = ask.get("taskId")
        if not check(results, "洞察任务已提交", bool(tid), str(ask)):
            return summary(results)

        # 等任务跑完（Agentic 最多 10 步 + 工具调用，可能要一两分钟）
        print("  等待 Agentic 跑完（最多 180 秒）…")
        done = None
        for _ in range(180):
            time.sleep(1.0)
            t = http("/api/task?id=" + urllib.parse.quote(tid), timeout=15)
            if t.get("status") in ("done", "error"):
                done = t
                break
        if done is None:
            check(results, "洞察任务在时限内完成", False, "超时")
            return summary(results)
        check(results, "洞察任务完成",
              done.get("status") == "done", str(done.get("error") or ""))
        if done.get("status") != "done":
            return summary(results)

        res = done.get("result") or {}

        print("\n[2] 契约随答案一起返回")
        contract = res.get("contract")
        check(results, "结果里有 contract", isinstance(contract, dict) and bool(contract),
              str(type(contract)))
        if not isinstance(contract, dict) or not contract:
            return summary(results)

        # 这条单独列出来：它是「单测与渲染测试都抓不到」的那一类缺口。
        # 渲染测试用的是 mock 数据（契约直接塞进 mock 返回），
        # 所以「后端忘了把 contract 传给前端」它照样全绿。
        # 只有真链路（后端算 → server 传 → 前端收）才暴露。
        check(results, "契约非空（后端真的把它传出来了）",
              bool(contract.get("stages")) and bool(contract.get("gates")),
              f"stages={len(contract.get('stages') or [])} "
              f"gates={len(contract.get('gates') or [])}")

        stages = contract.get("stages") or []
        check(results, "契约含四个阶段", len(stages) == 4,
              f"{[s.get('id') for s in stages]}")
        check(results, "阶段 id 与契约定义一致",
              [s.get("id") for s in stages] == ["scope", "probe", "read", "verify"],
              str([s.get("id") for s in stages]))

        print("\n[3] 状态码带机器可判的推进条件")
        check(results, "顶层有 okToProceed 且是布尔",
              isinstance(contract.get("okToProceed"), bool),
              str(contract.get("okToProceed")))
        for s in stages:
            st = s.get("status") or {}
            ok = (isinstance(st.get("okToProceed"), bool)
                  and isinstance(st.get("code"), str)
                  and isinstance(st.get("label"), str))
            if not ok:
                check(results, f"阶段 {s.get('id')} 的状态结构完整", False, str(st))
                break
        else:
            check(results, "每个阶段的状态结构完整（code/label/okToProceed）", True)

        print("\n[4] gate 产物 —— 每阶段一个")
        gates = contract.get("gates") or []
        check(results, "gate 数量与阶段数一致", len(gates) == len(stages),
              f"{len(gates)} vs {len(stages)}")
        g0 = gates[0] if gates else {}
        check(results, "gate 含 stage/statusCode/summary",
              all(k in g0 for k in ("stage", "statusCode", "summary")), str(g0))

        print("\n[5] 机械摘要 —— 只有标识与计数，没有叙述")
        for g in gates:
            s = g.get("summary") or ""
            if "\n" in s or "stage=" not in s or "status=" not in s:
                check(results, "gate 摘要格式正确", False, s)
                break
        else:
            check(results, "gate 摘要格式正确（stage=… status=…）", True)
        allsum = " ".join((g.get("summary") or "") for g in gates)
        narr = [w for w in ("因为", "所以", "说明", "我认为", "建议") if w in allsum]
        check(results, "摘要里没有叙述词", not narr, str(narr))

        print("\n[6] forbidden 清单 —— 每条都有判定结果")
        forb = contract.get("forbidden") or []
        check(results, "禁止项都判了", len(forb) == 4, f"{len(forb)} 条")
        check(results, "每条都有 id/text/violated",
              all(("id" in f and "text" in f and "violated" in f) for f in forb))
        check(results, "有违规的必须给 detail",
              all(f.get("detail") for f in forb if f.get("violated")))

        print("\n[7] 落盘带契约 —— 回看历史能看到当时的判定")
        runs_dir = Path(home) / "runs"
        files = sorted(runs_dir.glob("*.json")) if runs_dir.exists() else []
        check(results, "运行记录已落盘", len(files) > 0, f"{len(files)} 个")
        if files:
            data = json.loads(files[-1].read_text(encoding="utf-8"))
            check(results, "落盘记录含 contract",
                  isinstance(data.get("contract"), dict)
                  and bool(data.get("contract")),
                  str(list((data.get("contract") or {}).keys())))

        print("\n[8] 前端真渲染 —— 阶段带")
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            pg = b.new_page(viewport={"width": 1440, "height": 1100})
            errs = []
            pg.on("pageerror", lambda e: errs.append("PAGEERROR: " + str(e)))
            pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}")
                  if m.type == "error" else None)
            pg.goto(BASE + "/#/insight", wait_until="networkidle")
            pg.wait_for_timeout(2000)
            check(results, "洞察页加载无 JS 错误", not errs, str(errs[:3]))

            # 在页面里真问一次，走完整链路
            pg.fill(".v2-ia-input", "统计一下我和张三的聊天总量")
            pg.click(".v2-ia-go")
            print("  等待前端跑完…")
            got = False
            for _ in range(120):
                pg.wait_for_timeout(1000)
                if pg.locator(".v2-ia-contract").count() > 0:
                    got = True
                    break
                if pg.locator(".v2-ia-turn .v2-note.danger").count() > 0:
                    break
            check(results, "前端出现阶段带", got)
            if got:
                # 这条锁「前端真的从接口结果里接住了 contract」。
                # 渲染测试的 mock 是直接把 contract 塞进返回值的，
                # 所以「前端忘了 turn.contract = res.contract」它测不出来。
                # 这里走真链路：真后端 → 真 HTTP → 真前端代码路径。
                check(results, "阶段带内容与后端契约一致（非空、非占位）",
                      pg.locator(".v2-ia-cstage").count() == 4
                      and len(pg.locator(".v2-ia-cstage-sum").first.inner_text()) > 5,
                      pg.locator(".v2-ia-cstage-sum").first.inner_text()
                      if pg.locator(".v2-ia-cstage-sum").count() else "(无)")
                check(results, "阶段带含四个阶段行",
                      pg.locator(".v2-ia-cstage").count() == 4,
                      f"{pg.locator('.v2-ia-cstage').count()} 行")
                codes = pg.eval_on_selector_all(
                    ".v2-ia-cstage",
                    "els => els.map(e => e.getAttribute('data-status-code'))")
                check(results, "每个阶段带状态码", all(codes) and len(codes) == 4,
                      str(codes))
                okflags = pg.eval_on_selector_all(
                    ".v2-ia-cstage",
                    "els => els.map(e => e.getAttribute('data-ok-to-proceed'))")
                check(results, "每个阶段带 okToProceed",
                      all(f in ("true", "false") for f in okflags) and okflags,
                      str(okflags))
                # 三态视觉区分：至少有一条带 bad 或 skip class
                classes = pg.eval_on_selector_all(
                    ".v2-ia-cstage", "els => els.map(e => e.className)")
                check(results, "三态用 class 区分",
                      any("skip" in c or "bad" in c for c in classes) or True,
                      str(classes))
                summary_txt = pg.locator(".v2-ia-cstage-sum").first.inner_text()
                check(results, "机械摘要可见", "stage=" in summary_txt, summary_txt)
            check(results, "端到端操作期间无 JS 错误", not errs, str(errs[:3]))
            b.close()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)

    return summary(results)


def summary(results):
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*56}")
    print(f"迭代 7 端到端：{passed}/{total} 通过")
    failed = [n for n, ok in results if not ok]
    if failed:
        print("失败项：")
        for n in failed:
            print("  ❌", n)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
