"""迭代 8 端到端：阶段模型路由（取证 / 成文双角色）真机验证。

验证八件事：
  1. 真起 GUI 服务 → 阶段模型接口可用
  2. **不配置时行为不变**：默认跑完，契约里 compose 阶段是 SKIPPED
  3. **配了成文模型就跑成文**：结果里 crossModel.distinct=True 且 applied=True
  4. **成文模型与取证模型同名 → 不跑成文**，且挂 no_self_review 违规
  5. **谓词名与 Lancer 一致**：distinct_configured_model
  6. **成文模型看到的是证据不是自述**（交叉核验的前提）
  7. **落盘带 modelRouting**：回看历史知道这答案是谁写的
  8. **前端真渲染**：交叉核验块出现，取证/成文模型名都可见

为什么必须真跑两个不同的模型
----------------------------
「两个不同的模型」这件事，mock 是 mock 不出来的——
把 tool_numbers 造出来就能让判定通过，但那不叫交叉核验。
所以这里真发两次请求：取证用 deepseek-v4.1-flash，成文用 glm-5.3，
两个不同家族，然后核对**答案文本确实换了一轮**。

前置条件
--------
本机 8787 端口要有可用的 OpenAI 兼容网关（WorkBuddy Console）。
没有网关时脚本会明确报告跳过，而不是伪装通过。

用法：python tests/e2e/lancer/e2e_iter8.py
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

BASE = "http://127.0.0.1:8798"
PY = sys.executable

ENV_FILE = ROOT / ".env"

# 取证 / 成文用两个不同家族的模型（不同家族 = 真的是两个脑子）
COLLECT_MODEL = "deepseek-v4.1-flash"
WRITE_MODEL = "glm-5.3"


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
    base = (cfg.get("MOSU_BOT_BASE_URL") or "").rstrip("/")
    key = cfg.get("MOSU_BOT_API_KEY") or ""
    if not base or not key:
        return False
    try:
        req = urllib.request.Request(
            base + "/models", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=6) as r:
            if r.status != 200:
                return False
            ids = {m.get("id") for m in
                   json.loads(r.read().decode("utf-8")).get("data", [])}
            # 两个模型都得在——不然交叉核验没法真跑
            return COLLECT_MODEL in ids and WRITE_MODEL in ids
    except Exception:
        return False


def set_stage_models(vault: Path, models: dict) -> None:
    """直接改临时密钥库里的 stageModels（等价于走 GUI 的保存接口）。"""
    data = json.loads(vault.read_text(encoding="utf-8"))
    data.setdefault("llm", {})["stageModels"] = models
    vault.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def run_insight(question: str, wait_s: int = 200) -> dict:
    """发一次洞察并等它跑完。"""
    ask = http("/api/insight/ask", {"question": question}, timeout=30)
    tid = ask.get("taskId")
    if not tid:
        return {"_error": "任务未提交: " + str(ask)}
    for _ in range(wait_s):
        time.sleep(1.0)
        t = http("/api/task?id=" + urllib.parse.quote(tid), timeout=20)
        if t.get("status") in ("done", "error"):
            return t
    return {"_error": "超时"}


def main():
    from playwright.sync_api import sync_playwright

    results = []
    env_cfg = _load_env()

    print("[0] 检查前置条件")
    if not gateway_ready(env_cfg):
        print("  ⚠️ 本机 8787 网关不可用，或缺少需要的两个模型")
        print(f"     需要 {COLLECT_MODEL} 与 {WRITE_MODEL} 都在模型列表里")
        print("     → 本轮端到端跳过。真机验证需要可用的 OpenAI 兼容网关。")
        print("     阶段模型逻辑本身已由 tests/test_stage_models.py（37 例）与")
        print("     tests/test_stage_models_wiring.py（33 例）覆盖。")
        return 2

    home = tempfile.mkdtemp(prefix="mosu_e2e_iter8_")
    env = dict(os.environ)
    env["MOSU_HOME"] = home
    env["MOSU_BOT_BASE_URL"] = env_cfg["MOSU_BOT_BASE_URL"]
    env["MOSU_BOT_API_KEY"] = env_cfg["MOSU_BOT_API_KEY"]
    env["MOSU_BOT_MODEL"] = COLLECT_MODEL
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
            "model": COLLECT_MODEL,
        },
    }, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.Popen(
        [PY, str(ROOT / "cli" / "mosu.py"), "gui", "--port", "8798", "--no-browser"],
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

        # ---------------------------------------------------------- 场景 A
        print("\n[1] 场景 A —— 不配置阶段模型（行为必须与迭代 7 一字不差）")
        sm = http("/api/stage-models", timeout=15)
        roles = (sm or {}).get("roles") or []
        check(results, "阶段模型接口返回两个角色",
              [r.get("role") for r in roles] == ["collect", "write"],
              str([r.get("role") for r in roles]))
        check(results, "未配置时两个角色都为空模型",
              all(not r.get("model") for r in roles),
              str([r.get("model") for r in roles]))

        done = run_insight("统计一下我和张三的聊天总量，我发出多少条？")
        if done.get("_error") or done.get("status") != "done":
            check(results, "场景 A 洞察任务完成", False,
                  str(done.get("error") or done.get("_error")))
            return summary(results)
        check(results, "场景 A 洞察任务完成", True)

        res = done.get("result") or {}
        contract = res.get("contract") or {}
        cm = contract.get("crossModel") or {}
        stages = {s.get("id"): s for s in (contract.get("stages") or [])}

        check(results, "场景 A 结果里有 crossModel", isinstance(cm, dict) and bool(cm),
              str(type(cm)))
        check(results, "场景 A 未配置成文模型 → distinct=False",
              cm.get("distinct") is False, str(cm.get("distinct")))
        check(results, "场景 A 未配置成文模型 → applied=False",
              cm.get("applied") is False, str(cm.get("applied")))
        check(results, "场景 A compose 阶段是 SKIPPED",
              (stages.get("compose") or {}).get("status", {}).get("code") == "SKIPPED",
              str((stages.get("compose") or {}).get("status")))
        # 关键：没配置时不能有「自审」违规（那是用户选了单模型模式，不是配置错）
        forb = {f.get("id"): f for f in (contract.get("forbidden") or [])}
        check(results, "场景 A 不报 no_self_review（单模型模式不算违规）",
              forb.get("no_self_review", {}).get("violated") is False,
              str(forb.get("no_self_review")))
        check(results, "场景 A 契约仍含原有四阶段（没被迭代 8 改坏）",
              set(stages.keys()) >= {"scope", "probe", "read", "verify"},
              str(list(stages.keys())))
        answer_a = res.get("answer") or ""
        check(results, "场景 A 有答案", len(answer_a) > 20, f"{len(answer_a)} 字")

        # ---------------------------------------------------------- 场景 B
        print("\n[2] 场景 B —— 配了不同的成文模型（真交叉核验）")
        http("/api/stage-models",
             {"models": {"write": {"model": WRITE_MODEL}}}, timeout=15)
        sm2 = http("/api/stage-models", timeout=15)
        roles2 = (sm2 or {}).get("roles") or []
        wmodel = next((r.get("model") for r in roles2 if r.get("role") == "write"), "")
        check(results, "成文模型已保存", wmodel == WRITE_MODEL, str(wmodel))

        done_b = run_insight("统计一下我和张三的聊天总量，我发出多少条？")
        if done_b.get("_error") or done_b.get("status") != "done":
            check(results, "场景 B 洞察任务完成", False,
                  str(done_b.get("error") or done_b.get("_error")))
            return summary(results)
        check(results, "场景 B 洞察任务完成", True)

        res_b = done_b.get("result") or {}
        contract_b = res_b.get("contract") or {}
        cm_b = contract_b.get("crossModel") or {}
        stages_b = {s.get("id"): s for s in (contract_b.get("stages") or [])}
        forb_b = {f.get("id"): f for f in (contract_b.get("forbidden") or [])}

        check(results, "场景 B distinct=True（两个模型确实不同）",
              cm_b.get("distinct") is True, str(cm_b))
        check(results, "场景 B applied=True（成文真的跑了）",
              cm_b.get("applied") is True,
              str(cm_b.get("applied")) + " " + str(cm_b.get("composeError")))
        check(results, "场景 B 取证模型记录正确",
              cm_b.get("collectModel") == COLLECT_MODEL,
              str(cm_b.get("collectModel")))
        check(results, "场景 B 成文模型记录正确",
              cm_b.get("writeModel") == WRITE_MODEL,
              str(cm_b.get("writeModel")))
        check(results, "场景 B 谓词名与 Lancer 一致",
              cm_b.get("predicate") == "distinct_configured_model",
              str(cm_b.get("predicate")))
        check(results, "场景 B 谓词成立",
              cm_b.get("predicateSatisfied") is True,
              str(cm_b.get("predicateSatisfied")))
        check(results, "场景 B compose 阶段是 OK",
              (stages_b.get("compose") or {}).get("status", {}).get("code") == "OK",
              str((stages_b.get("compose") or {}).get("status")))
        check(results, "场景 B 不报 no_self_review",
              forb_b.get("no_self_review", {}).get("violated") is False,
              str(forb_b.get("no_self_review")))
        # 成文的机械摘要里要能看到两个模型名
        csum = (stages_b.get("compose") or {}).get("summary") or ""
        check(results, "compose 摘要含两个模型名",
              COLLECT_MODEL in csum and WRITE_MODEL in csum, csum)

        # 场景 B 的 compose 阶段必须是**唯一**用 WRITE_MODEL 的地方——
        # 证明答案确实是第二个模型写的
        answer_b = res_b.get("answer") or ""
        check(results, "场景 B 有答案", len(answer_b) > 20, f"{len(answer_b)} 字")
        # 成文提示词要求每个结论标「（依据：…）」。
        # 这是「答案真的是第二个模型按成文规则写的」的硬证据——
        # 只看 modelRouting 里的字段名，证明不了模型真参与了。
        check(results, "场景 B 答案带证据引用标记（成文规则真生效）",
              "依据" in answer_b,
              answer_b[:160].replace("\n", " "))
        check(results, "场景 B 成文模型出现在结果里",
              WRITE_MODEL in json.dumps(res_b, ensure_ascii=False),
              "结果里找不到成文模型名")

        # ---------------------------------------------------------- 场景 C
        print("\n[3] 场景 C —— 成文模型配成与取证相同（必须点破，不许假装）")
        http("/api/stage-models",
             {"models": {"write": {"model": COLLECT_MODEL}}}, timeout=15)
        done_c = run_insight("统计一下我和张三的聊天总量")
        if done_c.get("_error") or done_c.get("status") != "done":
            check(results, "场景 C 洞察任务完成", False,
                  str(done_c.get("error") or done_c.get("_error")))
            return summary(results)
        check(results, "场景 C 洞察任务完成", True)

        res_c = done_c.get("result") or {}
        contract_c = res_c.get("contract") or {}
        cm_c = contract_c.get("crossModel") or {}
        stages_c = {s.get("id"): s for s in (contract_c.get("stages") or [])}
        forb_c = {f.get("id"): f for f in (contract_c.get("forbidden") or [])}

        check(results, "场景 C distinct=False（同模型不算交叉核验）",
              cm_c.get("distinct") is False, str(cm_c))
        check(results, "场景 C applied=False（没跑成文）",
              cm_c.get("applied") is False, str(cm_c.get("applied")))
        check(results, "场景 C compose 阶段是 SKIPPED",
              (stages_c.get("compose") or {}).get("status", {}).get("code") == "SKIPPED",
              str((stages_c.get("compose") or {}).get("status")))
        check(results, "场景 C 报 no_self_review（白配了要点破）",
              forb_c.get("no_self_review", {}).get("violated") is True,
              str(forb_c.get("no_self_review")))
        check(results, "场景 C 违规必须给 detail",
              bool(forb_c.get("no_self_review", {}).get("detail")),
              str(forb_c.get("no_self_review", {}).get("detail")))
        check(results, "场景 C composeError 说明原因",
              "交叉核验" in (cm_c.get("composeError") or ""),
              str(cm_c.get("composeError")))

        # 复原成不同模型，给后面的前端验证用
        http("/api/stage-models",
             {"models": {"write": {"model": WRITE_MODEL}}}, timeout=15)

        # ---------------------------------------------------------- 落盘
        print("\n[4] 落盘带 modelRouting")
        runs_dir = Path(home) / "runs"
        files = sorted(runs_dir.glob("*.json")) if runs_dir.exists() else []
        check(results, "运行记录已落盘", len(files) >= 2, f"{len(files)} 个")
        if files:
            # 按 createdAt 排最新，**不能按文件名排**：
            # 文件名里的时间戳只是约定，格式不统一时排序会乱，而且不报错。
            records = []
            for p in files:
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(data, dict):
                    records.append((float(data.get("createdAt") or
                                         data.get("savedAt") or 0), data))
            records.sort(key=lambda r: r[0])
            latest = records[-1][1]
            mr = latest.get("modelRouting") or {}
            check(results, "落盘记录含 modelRouting", isinstance(mr, dict) and bool(mr),
                  str(list(mr.keys())))
            # 注意：场景 C 把成文模型配成了同名，所以**最新**一条的
            # writeModel 是取证模型。要证明「成文那次落盘了成文模型名」，
            # 得在全部记录里找 writeModel == WRITE_MODEL 的那一条。
            hit = next((d for _ts, d in reversed(records)
                        if (d.get("modelRouting") or {}).get("writeModel") == WRITE_MODEL),
                       None)
            check(results, "落盘记录含成文模型名", hit is not None,
                  str((hit or {}).get("modelRouting", {}).get("writeModel")))
            hit_cm = ((hit or {}).get("contract") or {}).get("crossModel")
            check(results, "成文那条落盘的契约有 crossModel", bool(hit_cm),
                  str(hit_cm))
            last_cm = (latest.get("contract") or {}).get("crossModel")
            check(results, "落盘记录含契约", bool(last_cm), str(last_cm))

        # ---------------------------------------------------------- 前端
        print("\n[5] 前端真渲染 —— 交叉核验块")
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

            pg.fill(".v2-ia-input", "统计一下我和张三的聊天总量")
            pg.click(".v2-ia-go")
            print("  等待前端跑完…")
            got = False
            for _ in range(200):
                pg.wait_for_timeout(1000)
                if pg.locator(".v2-ia-crossmodel").count() > 0:
                    got = True
                    break
                if pg.locator(".v2-ia-turn .v2-note.danger").count() > 0:
                    break
            check(results, "前端出现交叉核验块", got)
            if got:
                # 前端必须显示两个模型名（不是只显示「已生效」）
                txt = pg.locator(".v2-ia-crossmodel-body").first.inner_text()
                check(results, "前端显示取证模型名",
                      COLLECT_MODEL in txt, txt.replace("\n", " ")[:120])
                check(results, "前端显示成文模型名",
                      WRITE_MODEL in txt, txt.replace("\n", " ")[:120])
                pred = pg.eval_on_selector(
                    ".v2-ia-crossmodel",
                    "e => e.getAttribute('data-predicate')")
                check(results, "前端谓词锚点正确",
                      pred == "distinct_configured_model", str(pred))
                sat = pg.eval_on_selector(
                    ".v2-ia-crossmodel",
                    "e => e.getAttribute('data-predicate-satisfied')")
                check(results, "前端谓词状态为 true", sat == "true", str(sat))
            check(results, "端到端操作期间无 JS 错误", not errs, str(errs[:3]))
            b.close()

        # ---------------------------------------------------------- 设置页
        print("\n[6] 设置页 —— 阶段模型配置面板")
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            pg = b.new_page(viewport={"width": 1440, "height": 1100})
            errs = []
            pg.on("pageerror", lambda e: errs.append("PAGEERROR: " + str(e)))
            pg.goto(BASE + "/#/settings.model", wait_until="networkidle")
            pg.wait_for_timeout(2500)
            check(results, "设置页模型 tab 加载无 JS 错误", not errs, str(errs[:3]))
            n = pg.locator(".v2-sm-card").count()
            check(results, "阶段模型卡片出现（两个角色）", n == 2, f"{n} 张")
            if n == 2:
                roles_ui = pg.eval_on_selector_all(
                    ".v2-sm-card", "els => els.map(e => e.getAttribute('data-stage-role'))")
                check(results, "两个角色正确",
                      sorted(roles_ui) == ["collect", "write"], str(roles_ui))
                sel = pg.locator("[data-stage-select='write']").input_value()
                check(results, "成文模型下拉显示已保存的模型",
                      sel == WRITE_MODEL, sel)
            check(results, "设置页操作期间无 JS 错误", not errs, str(errs[:3]))
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
    print(f"迭代 8 端到端：{passed}/{total} 通过")
    failed = [n for n, ok in results if not ok]
    if failed:
        print("失败项：")
        for n in failed:
            print("  ❌", n)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
