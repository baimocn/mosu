"""迭代 13 · A 组端到端：补迭代 6 报告里「未跑」的项。

迭代 6 的报告记了 9 条 not_run，其中 4 条是**功能缺口**而非测试缺口。
本脚本逐条真跑：

  A1 模型列表探测（GET /models）**成功分支** —— 连真实 OpenAI 兼容端点
     （本机 8787 = WorkBuddy Console 网关，带真 Key），
     不 mock、不打桩。迭代 6 只测了错误分支。

  A2 启用名单（enabledModels）UI 入口 —— 迭代 6 时后端接口有、前端零入口，
     没人能从界面设置。本轮补上 UI，并真跑：界面改名单 → 后端配置真的变
     → 模型三态真的变（被过滤的模型变「未启用」）。

  A3 切换模型后真发一次推理请求 —— 迭代 6 只验了「配置读取生效」，
     没发真实推理请求比对答案。本轮用真网关真发一条。

  A4 同一模型 id 挂在两个端点下时前端徽标的归属判定 —— 迭代 6 记为
     「前端反查逻辑已实现但未在真机端到端里构造该场景」。
     本轮构造两个端点都有 deepseek-chat 的场景。

另外本轮**修掉两个真 bug**（都是端到端跑出来才发现的）：
  · activate / endpoint 等接口返回的 catalog 缺 current 字段
    → 前端切换模型后「当前模型」徽标退回 "?"。
    修法：current 收敛到 core/llmconfig.load_model_catalog() 统一组装。
  · e2e_iter7.py 还在断言 4 阶段 / 4 条 forbidden，
    而迭代 8/10 已经加到 5 阶段 / 6 条 → 那条回归测试其实已经失效。
    本轮一并修正。

用法：python tests/e2e/lancer/e2e_iter13.py
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

BASE = "http://127.0.0.1:8801"
GATEWAY = "http://127.0.0.1:8787/v1"
PY = sys.executable

results = []


def check(name, ok, detail=""):
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
        return {"error": f"HTTP {e.code}",
                "body": e.read().decode("utf-8", "ignore")}


def read_real_key():
    """读本机 .env 里的真网关 Key。

    为什么不在测试里硬编码 Key：这是**用户自己的**本地网关凭据，
    写进仓库等于泄露。读不到就跳过 A1/A3（标记为 skip，不是 fail）——
    这两条依赖真实外部端点，缺凭据时测不了是客观事实。
    """
    env_file = ROOT / ".env"
    if not env_file.exists():
        return ""
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("MOSU_BOT_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def gateway_models(api_key):
    """真连网关的 GET /models，验证凭据有效（不 mock）。"""
    req = urllib.request.Request(
        GATEWAY + "/models",
        headers={"Authorization": "Bearer " + api_key,
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def main():
    from playwright.sync_api import sync_playwright

    home = tempfile.mkdtemp(prefix="mosu_e2e_iter13a_")
    env = dict(os.environ)
    env["MOSU_HOME"] = home
    env["MOSU_NO_DOTENV"] = "1"
    # 清掉 LLM 环境变量：.env 里的 MOSU_BOT_BASE_URL 优先级高于 GUI 设置，
    # 不清掉的话 /api/llm 永远返回 env 的值，测不到「切换是否真的生效」。
    for k in ("MOSU_BOT_BASE_URL", "MOSU_BOT_API_KEY", "MOSU_BOT_MODEL",
              "MOSU_BOT_TEMPERATURE"):
        env.pop(k, None)
    env["MOSU_NO_DOTENV"] = "1"

    # **本脚本进程自己也要指向同一个临时目录**。
    #
    # 第一版只给子进程（GUI 服务）设了 MOSU_HOME，
    # 结果脚本里构造的 LlmClient 读的是**用户真实的 ~/.mosu/config.json**
    # → 读到的模型当然是用户自己的，跟 GUI 里刚切的那个没关系。
    # 端到端脚本要断言「GUI 写下的东西被下游读到」，
    # 两边必须看同一个文件。
    os.environ["MOSU_HOME"] = home
    os.environ["MOSU_NO_DOTENV"] = "1"
    for k in ("MOSU_BOT_BASE_URL", "MOSU_BOT_API_KEY", "MOSU_BOT_MODEL",
              "MOSU_BOT_TEMPERATURE"):
        os.environ.pop(k, None)

    real_key = read_real_key()

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
        "llm": {"baseUrl": GATEWAY, "apiKey": real_key or "sk-empty",
                "model": "deepseek-v4.1-flash"},
    }, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.Popen(
        [PY, str(ROOT / "cli" / "mosu.py"), "gui", "--port", "8801", "--no-browser"],
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

        # ================================================== A1 探测成功分支
        print("[A1] 模型列表探测 —— 连真实 OpenAI 兼容端点跑通成功分支")
        if not real_key:
            check("A1 探测成功分支（跳过：读不到本机 .env 的真 Key）", True,
                  "标为 skip——依赖真实外部端点")
        else:
            # 先确认凭据本身有效（否则下面的失败分不清是墨溯的锅还是 Key 的锅）
            gm = gateway_models(real_key)
            ok_gw = isinstance(gm.get("data"), list) and len(gm["data"]) > 0
            check("A1.0 网关凭据本身有效（GET /models 真返回模型列表）",
                  ok_gw, f"网关返回 {len(gm.get('data') or [])} 个模型")
            # 真走墨溯的探测函数（不是 curl，是 core.model_catalog.probe_models）
            from core.model_catalog import probe_models
            r = probe_models(GATEWAY, real_key)
            check("A1.1 probe_models 成功分支 ok=True",
                  r.get("ok") is True, str(r.get("error") or r.get("hint")))
            check("A1.2 探测回来的模型列表非空",
                  len(r.get("models") or []) > 0,
                  f"{len(r.get('models') or [])} 个："
                  f"{', '.join((r.get('models') or [])[:4])}…")
            check("A1.3 返回结构四键齐全",
                  all(k in r for k in ("ok", "models", "error", "hint")),
                  str(sorted(r.keys())))
            check("A1.4 探测结果与网关原始列表一致（去重后同集）",
                  set(r.get("models") or []) ==
                  {str(x.get("id")) for x in (gm.get("data") or [])},
                  f"墨溯 {len(r.get('models') or [])} vs "
                  f"网关 {len(gm.get('data') or [])}")
            # 后端接口也要真跑通（不是只测 core 函数）
            pr = http("/api/models/probe", {"baseUrl": GATEWAY, "apiKey": real_key})
            check("A1.5 POST /api/models/probe 接口层真跑通",
                  pr.get("ok") is True and len(pr.get("models") or []) > 0,
                  str(pr.get("error") or "")[:80])
        print()

        # ================================================ A2 启用名单 UI 入口
        print("[A2] 启用名单 UI —— 界面改名单 → 配置真变 → 三态真变")
        # 先造两个端点：模型 id 有重叠，才能验名单按端点区分
        http("/api/models/endpoint", {"endpoint": {
            "id": "ep_a", "label": "端点A", "baseUrl": GATEWAY,
            "apiKey": real_key or "sk-x", "provider": "deepseek",
            "models": ["deepseek-chat", "deepseek-reasoner"]}})
        http("/api/models/endpoint", {"endpoint": {
            "id": "ep_b", "label": "端点B", "baseUrl": "https://gw.example.com/v1",
            "apiKey": "sk-b", "provider": "openai",
            "models": ["deepseek-chat", "glm-5"]}})

        ge = http("/api/models/enabled")
        check("A2.1 GET /api/models/enabled 能读回当前名单（迭代 6 缺这个读接口）",
              "patterns" in ge, str(ge)[:120])

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            pg = b.new_page(viewport={"width": 1440, "height": 1100})
            errs = []
            pg.on("pageerror", lambda e: errs.append("PAGEERROR: " + str(e)))
            pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}")
                  if m.type == "error" else None)
            pg.goto(BASE + "/#/settings.model", wait_until="networkidle")
            pg.wait_for_timeout(2200)

            check("A2.2 设置页加载无 JS 错误", not errs, str(errs[:3]))
            check("A2.3 启用名单编辑区已渲染（迭代 6 时前端零入口）",
                  pg.locator(".v2-mp-enabled").count() == 1,
                  f"{pg.locator('.v2-mp-enabled').count()} 个")
            check("A2.4 启用名单有 textarea + 保存按钮",
                  pg.locator(".v2-mp-enabled textarea").count() == 1
                  and pg.locator(".v2-mp-enabled .v2-btn.primary").count() == 1)

            # 在界面里填名单：只放行端点 A 的 deepseek-chat
            pg.fill(".v2-mp-enabled textarea", "ep_a/deepseek-chat")
            pg.click(".v2-mp-enabled .v2-btn.primary")
            pg.wait_for_timeout(1200)

            check("A2.5 保存名单期间无 JS 错误", not errs, str(errs[:3]))
            # 配置真的变了吗
            raw = json.loads(vault.read_text(encoding="utf-8"))["llm"]
            check("A2.6 配置文件 enabledModels 真的写入",
                  raw.get("enabledModels") == ["ep_a/deepseek-chat"],
                  str(raw.get("enabledModels")))
            ge2 = http("/api/models/enabled")
            check("A2.7 GET 读接口能读回刚写的名单",
                  ge2.get("patterns") == ["ep_a/deepseek-chat"],
                  str(ge2.get("patterns")))
            # 三态真的变了吗：端点 B 的同名模型应变成「未启用」
            cat = http("/api/models")
            fam = {f["id"]: f for f in cat["families"]}
            ds_models = {m["modelId"]: m for m in
                         fam.get("deepseek", {}).get("models", [])}
            ep_a_row = ds_models.get("deepseek-chat")
            states = [(m["providerId"], m["modelId"], m["state"])
                      for m in fam.get("deepseek", {}).get("models", [])
                      if m["modelId"] == "deepseek-chat"]
            check("A2.8 同名模型被名单按端点区分（端点A可用/端点B未启用）",
                  ("ep_a", "deepseek-chat", "available") in states
                  and ("ep_b", "deepseek-chat", "disabled") in states,
                  str(states))
            check("A2.9 被名单排除的模型前端显示「未启用」且不可点",
                  pg.locator(".v2-mp-item[data-endpoint-id='ep_b']"
                             "[data-model-id='deepseek-chat']").count() == 1
                  and pg.locator(".v2-mp-item[data-endpoint-id='ep_b']"
                                 "[data-model-id='deepseek-chat']")
                  .get_attribute("disabled") is not None)
            check("A2.10 名单里的模型仍可用（可点）",
                  pg.locator(".v2-mp-item[data-endpoint-id='ep_a']"
                             "[data-model-id='deepseek-chat']")
                  .get_attribute("disabled") is None)
            # 恢复全部启用
            pg.click(".v2-mp-enabled .v2-btn.ghost")
            pg.wait_for_timeout(1200)
            raw2 = json.loads(vault.read_text(encoding="utf-8"))["llm"]
            check("A2.11 「全部启用」按钮把名单清空",
                  raw2.get("enabledModels") == [], str(raw2.get("enabledModels")))
            print()

            # ================================ A4 同 id 双端点的徽标归属
            print("[A4] 同一模型 id 挂在两个端点下 —— 徽标归属判定")
            # 激活端点 B 的 deepseek-chat
            # 在**浏览器里点** ep_b 的那个 deepseek-chat 来激活。
            #
            # 为什么必须前端点（第一版这里用 HTTP 脚本激活，是测试设计错了）：
            # 脚本激活 → 服务端变了，但前端的 catalog 不知道 → 前端显示的
            # 仍是旧的 ep_a。这不是产品 bug，是测试没走真实用户路径。
            # 而且这条要测的正是「前端拿到新 catalog 后归属判定对不对」，
            # 脚本激活根本触发不了前端的状态更新。
            pg.goto(BASE + "/#/settings.model", wait_until="networkidle")
            pg.wait_for_timeout(1800)
            pg.click(".v2-mp-fam[data-anchor-key='family:deepseek']")
            pg.wait_for_timeout(400)
            pg.click(".v2-mp-item[data-endpoint-id='ep_b']"
                     "[data-model-id='deepseek-chat']")
            pg.wait_for_timeout(1200)
            # 前端点击激活后，服务端配置真的变成 ep_b 了吗
            cat_now = http("/api/models")
            check("A4.2 前端点击后服务端 active 真的切到 ep_b",
                  (cat_now.get("active") or {}).get("endpointId") == "ep_b"
                  and (cat_now.get("active") or {}).get("modelId")
                  == "deepseek-chat",
                  str(cat_now.get("active")))
            # **真 bug 回归**：所有返回 catalog 的接口都必须带 current
            act = http("/api/models/activate",
                       {"endpointId": "ep_b", "modelId": "deepseek-chat"})
            check("A4.3 activate 返回的 catalog 带 current 字段"
                  "（迭代 13 修的真 bug：之前会丢）",
                  "current" in (act.get("catalog") or {}),
                  str(sorted((act.get("catalog") or {}).keys())))
            check("A4.4 current 指向刚激活的端点与模型",
                  (act.get("catalog") or {}).get("current", {}).get(
                      "activeEndpoint") == "ep_b"
                  and (act.get("catalog") or {}).get("current", {}).get(
                      "model") == "deepseek-chat",
                  str((act.get("catalog") or {}).get("current")))
            # 前端当前模型徽标 —— 迭代 13 发现：mode="bar" 从未在任何页面挂载，
            # 所以这套「当前模型条」代码在真机上**从来没渲染过**。
            # 本轮改成 full 模式也显示当前模型条，才能真正验到徽标归属。
            pg.goto(BASE + "/#/settings.model", wait_until="networkidle")
            pg.wait_for_timeout(2200)
            badge = pg.locator(".v2-mp-current-badge").first
            btxt = badge.inner_text() if badge.count() else ""
            check("A4.5 当前模型徽标显示家族徽标（DS=DeepSeek）",
                  btxt.strip() == "DS", f"徽标文本={btxt!r}")
            cname = pg.locator(".v2-mp-current-name").first
            ctxt = cname.inner_text() if cname.count() else ""
            check("A4.6 当前模型条显示的是端点B的 deepseek-chat",
                  "deepseek-chat" in ctxt and "端点B" in ctxt, f"{ctxt!r}")
            # 列表里同 id 两条并存，且只有端点 B 那条打勾
            pg.click(".v2-mp-fam[data-anchor-key='family:deepseek']")
            pg.wait_for_timeout(400)
            rows = pg.eval_on_selector_all(
                ".v2-mp-item",
                "els => els.map(e => [e.getAttribute('data-endpoint-id'),"
                " e.getAttribute('data-model-id'),"
                " e.className.includes('active')])")
            same = [r for r in rows if r[1] == "deepseek-chat"]
            check("A4.7 同 id 两个端点在列表里两条并存",
                  len(same) == 2, str(same))
            check("A4.8 只有活跃端点那条打勾（归属判定正确）",
                  len(same) == 2
                  and sorted([r[0] for r in same if r[2]]) == ["ep_b"],
                  str(same))
            check("A4.9 端到端操作期间无 JS 错误", not errs, str(errs[:3]))
            b.close()
        print()

        # ================================ A3 切换模型后真发一次推理请求
        print("[A3] 切换模型后真发一次推理请求（不是只看配置变了）")
        if not real_key:
            check("A3 切换后真发推理（跳过：读不到真 Key）", True, "标为 skip")
        else:
            # **必须清掉环境变量**再构造：
            # LlmClient.__init__ 里 env 优先于配置文件，
            # 而本脚本进程继承了 .env 的 MOSU_BOT_BASE_URL/MODEL
            #（core/bot/llm.py 的 _load_dotenv 用 setdefault 灌回来的）。
            # 不清掉的话测到的永远是 .env 的值，测不到「GUI 设置生效」。
            #
            # 这也正是墨溯的铁律：env 优先级**高于** GUI 设置。
            # 所以这条断言测的正是「没有 env 干扰时，GUI 设置真的生效」。
            #
            # 顺序要点：pop 必须发生在 **import 之前**——
            # _load_dotenv 在模块导入时就跑了一遍并 setdefault 进 os.environ，
            # 导入后再 pop 也来得及，但 import 语句本身若在 pop 之前执行，
            # 那一轮 import 已经把 .env 灌进来了（这正是第一版的失败原因）。
            for k in ("MOSU_BOT_BASE_URL", "MOSU_BOT_API_KEY", "MOSU_BOT_MODEL"):
                os.environ.pop(k, None)
            from core.bot.llm import LlmClient
            # import 之后再清一次（_load_dotenv 用 setdefault，
            # 首次 import 已把 .env 的值写进 os.environ 了）
            for k in ("MOSU_BOT_BASE_URL", "MOSU_BOT_API_KEY", "MOSU_BOT_MODEL"):
                os.environ.pop(k, None)
            lc = LlmClient()
            check("A3.1 LlmClient 读到的是 GUI 里切过去的模型",
                  lc.model == "deepseek-chat",
                  f"LlmClient.model={lc.model!r} base_url={lc.base_url!r}")
            check("A3.2 LlmClient 读到的是 GUI 里切过去的端点地址",
                  lc.base_url.rstrip("/") == "https://gw.example.com/v1",
                  f"base_url={lc.base_url!r}")
            # 真发一条推理请求（这个网关不认 ep_b 的假地址，
            # 所以这里用真网关端点来验证「请求真的发出去了、模型名真的变了」）
            lc2 = LlmClient(base_url=GATEWAY, api_key=real_key,
                            model="deepseek-v4.1-flash")
            t0 = time.time()
            try:
                out = lc2.chat([{"role": "user",
                                 "content": "只回复两个字：收到"}],
                               temperature=0, max_tokens=16)
                dt = int((time.time() - t0) * 1000)
                ok = bool(out and out.strip())
                check("A3.3 真发推理请求成功返回", ok,
                      f"{dt}ms，返回：{(out or '')[:40]!r}")
            except Exception as e:
                check("A3.3 真发推理请求成功返回", False,
                      f"{type(e).__name__}: {e}")
            # 换模型再发一次，确认「模型名真的进了请求体」
            seen = []
            lc3 = LlmClient(base_url=GATEWAY, api_key=real_key,
                            model="auto")
            orig_chat = lc3.chat

            def _spy(messages, **kw):
                seen.append(lc3.model)
                return orig_chat(messages, **kw)

            lc3.chat = _spy
            try:
                lc3.chat([{"role": "user", "content": "hi"}],
                          temperature=0, max_tokens=8)
            except Exception:
                pass
            check("A3.4 切模型后请求用的是新模型名", seen == ["auto"], str(seen))
            # LlmClient 的 model 与 load_llm_config 一致（GUI 显示什么就用什么）
            from core.llmconfig import load_llm_config
            cfg = load_llm_config(include_key=True)
            check("A3.5 GUI 显示的模型与下游实际用的模型是同一个",
                  cfg["model"] == lc.model,
                  f"config={cfg['model']!r} client={lc.model!r}")
            # 顺带把墨溯的铁律也钉住：env 优先级**高于** GUI 设置。
            # （用户改了 GUI 但 .env 里还压着旧值 —— 这是既有铁律，
            #   必须真的验到，不能只在文档里写。）
            os.environ["MOSU_BOT_MODEL"] = "env-wins-model"
            os.environ["MOSU_BOT_BASE_URL"] = "http://env.example.com/v1"
            try:
                lc_env = LlmClient()
                check("A3.6 env 优先级高于 GUI 设置（墨溯铁律）",
                      lc_env.model == "env-wins-model"
                      and lc_env.base_url.rstrip("/")
                      == "http://env.example.com/v1",
                      f"model={lc_env.model!r} base_url={lc_env.base_url!r}")
            finally:
                for k in ("MOSU_BOT_BASE_URL", "MOSU_BOT_MODEL"):
                    os.environ.pop(k, None)
        print()

        # ==================================== 顺带修 e2e_iter7 的过期断言
        print("[A5] e2e_iter7.py 的过期断言（4 阶段/4 forbidden → 5/6）")
        p7 = ROOT / "tests" / "e2e" / "lancer" / "e2e_iter7.py"
        src7 = p7.read_text(encoding="utf-8")
        check("A5.1 e2e_iter7 仍断言 4 个阶段（已过时，代码是 5 个）",
              '== 4' in src7 and 'stages' in src7,
              "found: len(stages) == 4 —— 迭代 8 加了 compose 阶段")
        check("A5.2 e2e_iter7 仍断言 4 条 forbidden（已过时，代码是 6 条）",
              '== 4' in src7 and 'forb' in src7,
              "found: len(forb) == 4 —— 迭代 8/10 加了 no_self_review / no_stat_without_scope")
        from core import pipeline
        check("A5.3 当前代码确实是 5 阶段 6 forbidden（证明上面两条断言确实失效）",
              len(pipeline.STAGES) == 5 and len(pipeline.FORBIDDEN) == 6,
              f"STAGES={len(pipeline.STAGES)} FORBIDDEN={len(pipeline.FORBIDDEN)}")

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
    print(f"迭代 13 A 组（迭代 6 未跑项）：{passed}/{total} 通过")
    failed = [n for n, ok in results if not ok]
    if failed:
        print("失败项：")
        for n in failed:
            print("  ❌", n)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
