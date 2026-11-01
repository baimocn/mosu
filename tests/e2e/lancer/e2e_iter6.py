"""迭代 6 端到端：模型目录（多端点 + 家族分组 + 三态 + 切换生效）真机验证。

验证六件事：
  1. 真起 GUI 服务 → /api/models 返回真实目录结构
  2. **家族归类按模型名**：自建网关（provider=openai）下的 claude-sonnet-4
     必须归到 Claude 家族，不归 GPT——这是 Lancer 的核心规则
  3. **三态真实存在**：没配 Key 的端点 → 其模型标「未配置」
  4. **切换模型真的生效**：调 /api/models/activate 后，
     /api/llm 的 model 字段必须跟着变（下游 bot/agent 读的就是它）
  5. **旧配置不被破坏**：改完多端点后，扁平字段仍然是有效模型名
  6. **前端真渲染**：设置页出现家族图标条，点家族切列表，
     未配置的模型按钮真的点不动

用法：python tests/e2e/lancer/e2e_iter6.py
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

# 仓库根：向上找带 core/ 的那层，不硬编码目录深度
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "core").is_dir() and (p / "cli").is_dir())
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8799"
PY = sys.executable


def check(results, name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def http(path, payload=None, method=None):
    url = BASE + path
    if payload is None:
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method or "POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode("utf-8", "ignore")}


def main():
    from playwright.sync_api import sync_playwright

    results = []
    home = tempfile.mkdtemp(prefix="mosu_e2e_iter6_")
    env = dict(os.environ)
    env["MOSU_HOME"] = home
    # 清掉可能压过配置的 LLM 环境变量。
    # 本机 .env 里配了 MOSU_BOT_BASE_URL（8787 网关），不清掉的话
    # /api/llm 永远返回 env 的值，测不到「切换模型是否真的生效」。
    for k in ("MOSU_BOT_BASE_URL", "MOSU_BOT_API_KEY", "MOSU_BOT_MODEL",
              "MOSU_BOT_TEMPERATURE"):
        env.pop(k, None)
    # .env 的路径是相对代码位置写死的，改不了 → 用开关禁用它。
    # （core/bot/llm.py 的 _load_dotenv 用 setdefault，
    #  光清环境变量没用，它会把 .env 的值又灌回来。）
    env["MOSU_NO_DOTENV"] = "1"

    print("[0] 准备独立配置目录:", home)
    # 写一个旧式扁平配置，验证向后兼容。
    # 账号信息从真实密钥库复制过来——GUI 启动时找不到微信数据目录会直接退出。
    # 只复制 dbPath/keyHex/wxid，**不复制用户的 LLM 配置**（本测试自己造）。
    real_vault = Path.home() / ".mosu" / "config.json"
    accounts = {}
    if real_vault.exists():
        try:
            accounts = json.loads(real_vault.read_text(encoding="utf-8")).get(
                "accounts") or {}
        except Exception:
            accounts = {}
    vault = Path(home) / "config.json"
    payload = {
        "accounts": accounts,
        "current": (list(accounts.keys())[0] if accounts else ""),
        "llm": {
            "baseUrl": "https://api.deepseek.com/v1",
            "apiKey": "sk-legacy-fake-key",
            "model": "deepseek-chat",
        },
    }
    vault.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.Popen(
        [PY, str(ROOT / "cli" / "mosu.py"), "gui", "--port", "8799", "--no-browser"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        # 等服务起来
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
        print("\n[1] GET /api/models —— 旧配置也要能出目录")
        cat = http("/api/models")
        check(results, "目录接口返回结构完整",
              isinstance(cat.get("families"), list)
              and isinstance(cat.get("endpoints"), list)
              and "current" in cat,
              f"families={len(cat.get('families') or [])} "
              f"endpoints={len(cat.get('endpoints') or [])}")
        check(results, "旧配置被合成为单个端点",
              len(cat.get("endpoints") or []) == 1,
              str([e["id"] for e in cat.get("endpoints") or []]))
        check(results, "旧配置下 current.model = deepseek-chat",
              cat.get("current", {}).get("model") == "deepseek-chat",
              str(cat.get("current")))

        print("\n[2] 建三个端点（含网关转发 Claude、无 Key 端点）")
        r1 = http("/api/models/endpoint", {"endpoint": {
            "id": "ep_ds", "label": "DeepSeek",
            "baseUrl": "https://api.deepseek.com/v1",
            "apiKey": "sk-ds-fake", "provider": "deepseek",
            "models": ["deepseek-chat", "deepseek-reasoner"]}})
        check(results, "端点 DeepSeek 已建", not r1.get("error"), str(r1.get("error", "")))
        r2 = http("/api/models/endpoint", {"endpoint": {
            "id": "ep_gw", "label": "自建网关",
            "baseUrl": "https://gw.example.com/v1",
            "apiKey": "sk-gw-fake", "provider": "openai",
            "models": ["claude-sonnet-4", "gpt-4o"]}})
        check(results, "端点 自建网关 已建", not r2.get("error"), str(r2.get("error", "")))
        r3 = http("/api/models/endpoint", {"endpoint": {
            "id": "ep_nokey", "label": "没配Key",
            "baseUrl": "https://api.qwen.com/v1",
            "apiKey": "", "provider": "qwen", "models": ["qwen-max"]}})
        check(results, "端点 没配Key 已建", not r3.get("error"), str(r3.get("error", "")))

        print("\n[3] 家族归类 —— 网关转发的 Claude 必须归 Claude")
        cat = http("/api/models")
        fam = {f["id"]: f for f in cat["families"]}
        claude_models = [m["modelId"] for m in fam.get("claude", {}).get("models", [])]
        gpt_models = [m["modelId"] for m in fam.get("openai", {}).get("models", [])]
        check(results, "claude-sonnet-4 归到 Claude 家族",
              "claude-sonnet-4" in claude_models, str(claude_models))
        check(results, "claude-sonnet-4 没有被误归到 GPT 家族",
              "claude-sonnet-4" not in gpt_models, str(gpt_models))
        check(results, "deepseek-chat 归到 DeepSeek 家族",
              "deepseek-chat" in [m["modelId"]
                                  for m in fam.get("deepseek", {}).get("models", [])])

        print("\n[4] 三态 —— 没配 Key 的端点，其模型标未配置")
        qwen_states = {m["modelId"]: m["state"]
                       for m in fam.get("qwen", {}).get("models", [])}
        check(results, "qwen-max 状态为 unconfigured",
              qwen_states.get("qwen-max") == "unconfigured", str(qwen_states))
        check(results, "可用计数正确",
              cat.get("availableModels") == 4 and cat.get("totalModels") == 5,
              f"{cat.get('availableModels')}/{cat.get('totalModels')}")

        print("\n[5] 切换模型 —— 下游读到的必须是新模型")
        act = http("/api/models/activate",
                   {"endpointId": "ep_gw", "modelId": "claude-sonnet-4"})
        check(results, "激活接口成功", act.get("activated") is True, str(act.get("error", "")))
        llm = http("/api/llm")
        check(results, "/api/llm 的 model 已变为 claude-sonnet-4",
              llm.get("model") == "claude-sonnet-4", str(llm.get("model")))
        check(results, "/api/llm 的 baseUrl 已变为网关地址",
              llm.get("baseUrl") == "https://gw.example.com/v1",
              str(llm.get("baseUrl")))
        check(results, "/api/llm 的 source 标记为 active",
              llm.get("source") == "active", str(llm.get("source")))
        # 扁平字段也要跟着变（老代码读它）
        raw = json.loads(vault.read_text(encoding="utf-8"))["llm"]
        check(results, "配置文件扁平字段同步更新",
              raw.get("model") == "claude-sonnet-4"
              and raw.get("baseUrl") == "https://gw.example.com/v1",
              f"model={raw.get('model')} baseUrl={raw.get('baseUrl')}")

        print("\n[6] 前端真渲染 —— 家族条 / 切列表 / 未配置不可点")
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            pg = b.new_page(viewport={"width": 1440, "height": 960})
            errs = []
            pg.on("pageerror", lambda e: errs.append("PAGEERROR: " + str(e)))
            pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}")
                  if m.type == "error" else None)
            pg.goto(BASE + "/#/settings.model", wait_until="networkidle")
            pg.wait_for_timeout(2200)
            check(results, "设置页加载无 JS 错误", not errs, str(errs[:3]))
            check(results, "模型选择器已渲染",
                  pg.locator(".v2-mp-fam").count() > 0,
                  f"家族按钮 {pg.locator('.v2-mp-fam').count()} 个")
            check(results, "端点列表已渲染",
                  pg.locator(".v2-mp-ep").count() == 3,
                  f"端点 {pg.locator('.v2-mp-ep').count()} 个")
            # 家族按钮的语义锚点
            keys = pg.eval_on_selector_all(
                ".v2-mp-fam", "els => els.map(e => e.getAttribute('data-anchor-key'))")
            check(results, "家族按钮带 data-anchor-key",
                  "family:claude" in keys and "family:deepseek" in keys, str(keys))
            # 点 Claude 家族 → 列表换成 claude
            pg.click(".v2-mp-fam[data-anchor-key='family:claude']")
            pg.wait_for_timeout(400)
            ids = pg.eval_on_selector_all(
                ".v2-mp-item", "els => els.map(e => e.getAttribute('data-model-id'))")
            check(results, "点家族后列表切换到 Claude",
                  ids == ["claude-sonnet-4"], str(ids))
            # 点 Qwen 家族 → 未配置模型 disabled
            pg.click(".v2-mp-fam[data-anchor-key='family:qwen']")
            pg.wait_for_timeout(400)
            states = pg.eval_on_selector_all(
                ".v2-mp-item",
                "els => els.map(e => [e.getAttribute('data-model-id'),"
                " e.getAttribute('data-state'), e.disabled])")
            check(results, "未配置模型按钮不可点",
                  any(s[0] == "qwen-max" and s[1] == "unconfigured" and s[2]
                      for s in states), str(states))
            label = pg.locator(".v2-mp-item-state").first.inner_text()
            check(results, "状态带文字标签", "未配置" in label, label)
            # 点 DeepSeek 可用模型 → 真的切换
            pg.click(".v2-mp-fam[data-anchor-key='family:deepseek']")
            pg.wait_for_timeout(400)
            pg.click(".v2-mp-item[data-model-id='deepseek-chat']")
            pg.wait_for_timeout(900)
            llm2 = http("/api/llm")
            check(results, "前端点击切换后后端模型真的变了",
                  llm2.get("model") == "deepseek-chat", str(llm2.get("model")))
            check(results, "端到端操作期间无 JS 错误", not errs, str(errs[:3]))
            b.close()

        print("\n[7] 删除端点 —— 删掉活跃端点要清空活跃指向")
        dele = http("/api/models/endpoint/delete", {"id": "ep_ds"})
        check(results, "端点删除成功", dele.get("deleted") is True, str(dele.get("error", "")))
        llm3 = http("/api/llm")
        check(results, "删除后 activeEndpoint 已清空",
              llm3.get("activeEndpoint", "") == "", str(llm3.get("activeEndpoint")))

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
    print(f"迭代 6 端到端：{passed}/{total} 通过")
    failed = [n for n, ok in results if not ok]
    if failed:
        print("失败项：")
        for n in failed:
            print("  ❌", n)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
