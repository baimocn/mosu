"""迭代 13 · C 组端到端：CLI 侧模型目录命令（迭代 6 最后一条功能缺口）。

迭代 6 的报告里最后一条 not_run 是：

    「墨溯 CLI 侧（mosu config / mosu llm 之类）的模型目录命令——
      本轮只做了 GUI 入口，CLI 未加」

这不是测试缺口，是**功能缺口**：迭代 6 把模型目录（多端点 + 家族分组 + 三态
+ 启用名单）做进了 GUI，却没给 CLI 留任何入口。后果是：

  · 写脚本 / 跑 CI / 用派活通道调模型时，**看不见也切不了**当前模型
  · GUI 能干的事 CLI 干不了 → 能力被锁在鼠标后面
  · 想批量切模型只能去点界面

本轮补上 `mosu models` 子命令组（目录总览 / 端点列表 / 切换 / 启用名单 /
探测 / 端点增删），并**真跑**验证：

  C1 目录总览       —— 隔离目录里造两个端点，真跑 CLI，验家族分组 / 三态 /
                       「当前」标记都在
  C2 切换模型       —— 真跑 `mosu models use`，验配置真变 + **扁平字段同步
                       回写**（bot/agent 读的就是扁平字段，不回写等于没切）
  C3 启用名单       —— 真跑 set → 验配置真写 + 三态真变（模型变「未启用」）
                       + `--show` 只读不改 + `--all` 显式清空
  C4 端点增删       —— 真跑 add → list → rm，验目录真变
  C5 JSON 输出      —— 验 `--format json` 输出是**纯 JSON**（不能被日志污染）
  C6 不依赖微信数据 —— 验 `mosu models` 在**没有 db-path / key** 时也能跑
                       （模型目录跟微信库毫无关系，不该被数据库卡住）
  C7 GUI 与 CLI 同源 —— 验 CLI 写完后，GUI 的 /api/models 读到的**是同一份**
                       （防止 CLI 另写一份配置，两边打架）

用法：python tests/e2e/lancer/e2e_iter13c.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "core").is_dir() and (p / "cli").is_dir())
sys.path.insert(0, str(ROOT))

PY = sys.executable
CLI = ROOT / "cli" / "mosu.py"
PORT = 8803
BASE = f"http://127.0.0.1:{PORT}"

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def run_cli(home, *args, expect_rc=0):
    """真跑一次 CLI（子进程），返回 (rc, stdout, stderr)。"""
    env = dict(os.environ)
    env["MOSU_HOME"] = home
    env["MOSU_NO_DOTENV"] = "1"
    for k in ("MOSU_BOT_BASE_URL", "MOSU_BOT_API_KEY", "MOSU_BOT_MODEL",
              "MOSU_BOT_TEMPERATURE", "MOSU_KEY_HEX", "MOSU_DB_PATH"):
        env.pop(k, None)
    p = subprocess.run([PY, str(CLI), *args], capture_output=True, text=True,
                       env=env, timeout=120, cwd=str(ROOT))
    return p.returncode, p.stdout, p.stderr


def read_cfg(home):
    p = Path(home) / "config.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def http(path, timeout=20):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def main():
    home = tempfile.mkdtemp(prefix="mosu_e2e_iter13c_")
    gui = None
    print("[0] 准备独立配置目录:", home)

    try:
        # ================================================== C1 目录总览
        print("\n[C1] `mosu models list` —— 家族分组 / 三态 / 当前标记")

        rc, out, err = run_cli(home, "models", "endpoint", "add", "ep_a",
                               "--base-url", "http://127.0.0.1:8787/v1",
                               "--label", "端点A",
                               "--api-key", "sk-test-a",
                               "--model", "deepseek-chat",
                               "--model", "deepseek-reasoner")
        check("C1.1 endpoint add 成功", rc == 0, f"rc={rc} {err.strip()[:120]}")
        rc, out, err = run_cli(home, "models", "endpoint", "add", "ep_b",
                               "--base-url", "http://127.0.0.1:8790/v1",
                               "--label", "端点B",
                               "--api-key", "sk-test-b",
                               "--model", "deepseek-chat")
        check("C1.2 第二个端点也加上了", rc == 0, f"rc={rc} {err.strip()[:120]}")

        rc, out, err = run_cli(home, "models", "list")
        check("C1.3 models list 退出码为 0", rc == 0, f"rc={rc}")
        check("C1.4 输出里有家族徽标（DeepSeek 归类）",
              "[DS]" in out and "DeepSeek" in out, out[:200].replace("\n", " | "))
        check("C1.5 输出里有两个端点",
              "端点A" in out and "端点B" in out,
              out[:200].replace("\n", " | "))
        check("C1.6 三态标签在（可用/未配置）",
              "可用" in out or "未配置" in out,
              out[:250].replace("\n", " | "))
        check("C1.7 显示了启用名单行",
              "启用名单" in out, "有" if "启用名单" in out else "没有")

        # ================================================== C2 切换模型
        print("\n[C2] `mosu models use` —— 真切换 + 扁平字段同步回写")
        rc, out, err = run_cli(home, "models", "use", "ep_b", "deepseek-chat")
        check("C2.1 use 退出码为 0", rc == 0, f"rc={rc} {err.strip()[:200]}")
        cfg = read_cfg(home)
        llm = cfg.get("llm") or {}
        check("C2.2 activeEndpoint 真的写进配置",
              llm.get("activeEndpoint") == "ep_b",
              f"activeEndpoint={llm.get('activeEndpoint')}")
        check("C2.3 activeModel 真的写进配置",
              llm.get("activeModel") == "deepseek-chat",
              f"activeModel={llm.get('activeModel')}")
        # 关键：bot/agent 的 LlmClient 读的是**扁平字段**，
        # 不同步回写的话，界面显示换了模型、实际跑的还是旧的。
        check("C2.4 扁平字段 baseUrl 同步回写",
              llm.get("baseUrl") == "http://127.0.0.1:8790/v1",
              f"baseUrl={llm.get('baseUrl')}")
        check("C2.5 扁平字段 model 同步回写",
              llm.get("model") == "deepseek-chat",
              f"model={llm.get('model')}")

        # 反向：切到不存在的端点 → 必须干净报错，不能静默
        rc, out, err = run_cli(home, "models", "use", "ep_nope", "x")
        check("C2.6 切到不存在的端点 → 非 0 退出码",
              rc != 0, f"rc={rc}")
        check("C2.7 报错是干净的中文提示（不是 traceback）",
              "Traceback" not in err and "端点不存在" in (out + err),
              (out + err).strip()[:160].replace("\n", " | "))

        # ================================================== C3 启用名单
        print("\n[C3] `mosu models enabled` —— 名单真写 + 三态真变")
        rc, out, err = run_cli(home, "models", "enabled",
                               "--pattern", "ep_a/deepseek-chat")
        check("C3.1 enabled 退出码为 0", rc == 0, f"rc={rc} {err.strip()[:150]}")
        cfg = read_cfg(home)
        pats = (cfg.get("llm") or {}).get("enabledModels") or []
        check("C3.2 名单真写进配置", pats == ["ep_a/deepseek-chat"], str(pats))

        rc, out, err = run_cli(home, "models", "list")
        check("C3.3 三态真变：被过滤的模型标成「未启用」",
              "未启用" in out, out[:300].replace("\n", " | "))
        check("C3.4 目录里显示了启用名单",
              "ep_a/deepseek-chat" in out,
              out[:300].replace("\n", " | "))

        # --show：只读，不能改
        before = read_cfg(home)
        rc, out, err = run_cli(home, "models", "enabled", "--show")
        after = read_cfg(home)
        check("C3.5 enabled --show 退出码为 0", rc == 0, f"rc={rc}")
        check("C3.6 --show 只读（配置没被改）", before == after,
              "配置变了" if before != after else "没变")
        check("C3.7 --show 输出了当前名单",
              "ep_a/deepseek-chat" in out, out[:200].replace("\n", " | "))

        # --all：显式清空
        rc, out, err = run_cli(home, "models", "enabled", "--all")
        check("C3.8 enabled --all 退出码为 0", rc == 0, f"rc={rc}")
        cfg = read_cfg(home)
        pats = (cfg.get("llm") or {}).get("enabledModels") or []
        check("C3.9 --all 把名单清空了", pats == [], str(pats))

        # 不带参数不带 --all 也不能瞎清（破坏性操作必须显式）
        run_cli(home, "models", "enabled", "--pattern", "ep_a/*")
        rc, out, err = run_cli(home, "models", "enabled")
        cfg = read_cfg(home)
        pats = (cfg.get("llm") or {}).get("enabledModels") or []
        check("C3.10 不带 --all 的空跑不会误清名单（破坏性操作需显式）",
              pats == ["ep_a/*"], str(pats))
        run_cli(home, "models", "enabled", "--all")

        # ================================================== C4 端点增删
        print("\n[C4] `mosu models endpoint rm` —— 真删 + 目录同步")
        rc, out, err = run_cli(home, "models", "endpoint", "rm", "ep_b")
        check("C4.1 endpoint rm 退出码为 0", rc == 0, f"rc={rc} {err.strip()[:150]}")
        cfg = read_cfg(home)
        eps = (cfg.get("llm") or {}).get("endpoints") or []
        check("C4.2 端点真被删了", all(e.get("id") != "ep_b" for e in eps),
              f"剩 {[e.get('id') for e in eps]}")
        check("C4.3 删掉活跃端点后 activeEndpoint 被清空",
              not (cfg.get("llm") or {}).get("activeEndpoint"),
              f"activeEndpoint={(cfg.get('llm') or {}).get('activeEndpoint')}")
        rc, out, err = run_cli(home, "models", "list")
        check("C4.4 list 里不再有端点B", "端点B" not in out,
              out[:200].replace("\n", " | "))

        # ================================================== C5 JSON 输出
        print("\n[C5] `--format json` —— 输出必须是纯 JSON")
        rc, out, err = run_cli(home, "models", "list", "--format", "json")
        check("C5.1 JSON 退出码为 0", rc == 0, f"rc={rc}")
        try:
            d = json.loads(out)
            ok_json = True
        except Exception as e:
            d = {}
            ok_json = False
        check("C5.2 stdout 能被 json.loads 解析（没被日志污染）", ok_json,
              out[:160].replace("\n", " | "))
        check("C5.3 JSON 里有 families 字段",
              isinstance(d.get("families"), list) and len(d["families"]) > 0,
              f"families={len(d.get('families') or [])}")
        check("C5.4 JSON 里有 current 字段（迭代 13 修的那个漂移）",
              isinstance(d.get("current"), dict),
              str(d.get("current"))[:120])
        check("C5.5 JSON 里有 enabledPatterns 字段",
              isinstance(d.get("enabledPatterns"), list),
              str(d.get("enabledPatterns"))[:120])
        check("C5.6 stderr 里的日志没有混进 stdout",
              "检测到旧密钥库" not in out,
              "混入了" if "检测到旧密钥库" in out else "干净")

        # ================================================== C6 不依赖微信数据
        print("\n[C6] 不依赖微信数据库")
        # 一个完全没有 db-path / key 的隔离目录，且不放任何微信数据
        bare = tempfile.mkdtemp(prefix="mosu_e2e_iter13c_bare_")
        try:
            rc, out, err = run_cli(bare, "models", "list")
            check("C6.1 没有密钥库时 models list 仍能跑", rc == 0,
                  f"rc={rc} {err.strip()[:160]}")
            check("C6.2 没有微信数据目录也不报错（提示为空目录）",
                  "当前模型" in out, out[:160].replace("\n", " | "))
            rc, out, err = run_cli(bare, "models", "endpoint", "add", "ep_x",
                                   "--base-url", "http://x/v1", "--model", "m1")
            check("C6.3 没密钥也能加端点（模型目录与微信数据无关）", rc == 0,
                  f"rc={rc} {err.strip()[:160]}")
        finally:
            shutil.rmtree(bare, ignore_errors=True)

        # ================================================== C7 GUI 与 CLI 同源
        print("\n[C7] GUI 与 CLI 读同一份配置")
        # 起一个真 GUI 服务（子进程），让它读同一个 MOSU_HOME
        env = dict(os.environ)
        env["MOSU_HOME"] = home
        env["MOSU_NO_DOTENV"] = "1"
        for k in ("MOSU_BOT_BASE_URL", "MOSU_BOT_API_KEY", "MOSU_BOT_MODEL",
                  "MOSU_BOT_TEMPERATURE"):
            env.pop(k, None)
        proc = subprocess.Popen(
            [PY, str(ROOT / "cli" / "mosu.py"), "gui", "--port", str(PORT),
             "--no-browser"],
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
            check("C7.0 GUI 服务启动", up, "" if up else "40 次轮询都没起来")
            if up:
                api = http("/api/models")
                check("C7.1 GUI /api/models 可达", "error" not in api,
                      str(api.get("error"))[:120])
                gui_eps = [e.get("id") for e in (api.get("endpoints") or [])]
                cfg = read_cfg(home)
                cli_eps = [e.get("id")
                           for e in ((cfg.get("llm") or {}).get("endpoints") or [])]
                check("C7.2 GUI 读到的端点与 CLI 写的完全一致",
                      gui_eps == cli_eps, f"GUI={gui_eps} CLI={cli_eps}")
                check("C7.3 GUI 的 catalog 里 current 也在（迭代 13 修的漂移）",
                      isinstance(api.get("current"), dict),
                      str(api.get("current"))[:120])
                # 用 CLI 改一次，GUI 立刻能读到 → 证明不是两份配置
                run_cli(home, "models", "enabled",
                        "--pattern", "ep_a/deepseek-reasoner")
                api2 = http("/api/models")
                check("C7.4 CLI 改完后 GUI 立刻读到（同一份配置）",
                      (api2.get("enabledPatterns") or []) == ["ep_a/deepseek-reasoner"],
                      str(api2.get("enabledPatterns"))[:120])
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        print()
        print("=" * 56)
        passed = sum(1 for _, ok in results if ok)
        total = len(results)
        print(f"迭代 13 C 组（CLI 侧模型目录命令）：{passed}/{total} 通过")
        if passed != total:
            print("\n失败项：")
            for name, ok in results:
                if not ok:
                    print(f"  ❌ {name}")
        return 0 if passed == total else 1
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
