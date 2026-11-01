"""迭代 9 端到端：派活通道的 stream / tail / stop（对标 Lancer Control API）。

验证九件事：
  1. 真起 GUI → 派活通道可用（stream/tail/stop 三个操作都认）
  2. **stream 真收到 NDJSON**：Content-Type 是 application/x-ndjson，
     一行一个 JSON，事件带全局单调 seq
  3. **stream 有内容**：派一个活，订阅端能实时看到工具轨迹
  4. **since 游标能补历史**：断线重连带上 seq，历史事件补齐、不重复
  5. **tail 能拿到答案**：末 N 条 + lastAssistant
  6. **stop 真打断**：正在跑的那轮被取消，答案不完整（取消 != 正常完成）
  7. **wait 能看到 cancelled**：取消后 wait 返回 cancelled 而不是干等到超时
  8. **凭证脱敏**：事件里不出现 API key
  9. **鉴权仍然有效**：token 不对 → 拒绝

为什么必须真跑
--------------
「stream 能不能实时收到事件」「stop 能不能真的打断」这两件事，
mock 都能造出「看起来对」的结果——但那不叫流式，也不叫取消。
所以这里真起 GUI、真发 HTTP、真跑模型、真收字节。

前置条件
--------
本机 8787 端口要有可用的 OpenAI 兼容网关（WorkBuddy Console）。
没有网关时脚本会明确报告跳过，而不是伪装通过。

用法：python tests/e2e/lancer/e2e_iter9.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# 仓库根：向上找带 core/ 的那层，不硬编码目录深度
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "core").is_dir() and (p / "cli").is_dir())
sys.path.insert(0, str(ROOT))

GUI_PORT = 8799
DISPATCH_PORT = 8797
BASE = f"http://127.0.0.1:{GUI_PORT}"
PY = sys.executable
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


def raw_stream(body: dict, token: str, port: int, timeout: float = 25.0,
               stop_after: int = 0):
    """发一个原始 NDJSON 流请求。

    返回 (头文本, [事件列表])。
    stop_after > 0 时收够这么多行就断开连接（模拟客户端 Ctrl+C）。
    """
    raw = json.dumps(body).encode("utf-8")
    head = (f"POST /v1 HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Authorization: Bearer {token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Accept: application/x-ndjson\r\n"
            f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
    buf = b""
    events = []
    header = ""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(head + raw)
        while True:
            try:
                d = s.recv(65536)
            except socket.timeout:
                break
            if not d:
                break
            buf += d
            if not header and b"\r\n\r\n" in buf:
                head_part, _, rest = buf.partition(b"\r\n\r\n")
                header = head_part.decode("latin-1")
                buf = rest
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line.decode("utf-8")))
                except ValueError:
                    continue
                if stop_after and len(events) >= stop_after:
                    return header, events
    return header, events


def main():
    results = []
    env_cfg = _load_env()

    print("[0] 检查前置条件")
    if not gateway_ready(env_cfg):
        print("  ⚠️ 本机 8787 网关不可用")
        print("     → 本轮端到端跳过。真机验证需要可用的 OpenAI 兼容网关。")
        print("     stream/tail/stop 的协议层已由 tests/test_dispatch_stream.py（62 例）覆盖。")
        return 2

    home = tempfile.mkdtemp(prefix="mosu_e2e_iter9_")
    env = dict(os.environ)
    env["MOSU_HOME"] = home
    env["MOSU_BOT_BASE_URL"] = env_cfg["MOSU_BOT_BASE_URL"]
    env["MOSU_BOT_API_KEY"] = env_cfg["MOSU_BOT_API_KEY"]
    env["MOSU_BOT_MODEL"] = env_cfg.get("MOSU_BOT_MODEL") or "deepseek-v4.1-flash"
    env["MOSU_DISPATCH_PORT"] = str(DISPATCH_PORT)
    env["MOSU_NO_DOTENV"] = "1"

    print("  独立配置目录:", home)
    # 密钥库要有 llm 段（GUI 的 LlmClient 读它）
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

        # 派活通道也要起来（端口独立）
        tok_path = Path(home) / "dispatch" / "control.token"
        for _ in range(20):
            if tok_path.exists():
                break
            time.sleep(0.5)
        if not check(results, "派活通道启动", tok_path.exists()):
            print("     stderr:", (proc.stdout.read(4000) or b"").decode("utf-8", "ignore")[:800]
                  if proc.stdout else "")
            return summary(results)
        token = tok_path.read_text(encoding="utf-8").strip()

        # ---------------------------------------------------------- 场景 A
        print("\n[1] 场景 A —— stream 的 NDJSON 协议")
        header, events = raw_stream({"op": "stream", "timeout": 3},
                                    token, DISPATCH_PORT, timeout=8)
        check(results, "stream 响应头是 application/x-ndjson",
              "application/x-ndjson" in header, header.split("\r\n")[0])
        # NDJSON 长度未知 → 不能有 Content-Length
        check(results, "NDJSON 响应无 Content-Length（长度未知）",
              "Content-Length" not in header,
              "有 Content-Length" if "Content-Length" in header else "")
        check(results, "stream 连上不报错", bool(header), header[:80])

        # ---------------------------------------------------------- 场景 B
        print("\n[2] 场景 B —— 真跑一个活，订阅端实时看到轨迹")
        from core import dispatch as dp

        # 关键：**先订阅、后派活**。
        # GUI 的 /api/insight/ask 走 GUI 自己的 TaskHub，不经过派活的
        # InsightRunner → 它的事件不进 EventJournal → 订阅端看不到。
        # 所以这里必须用**派活通道**自己的 send 来制造活动。
        collected: list[dict] = []
        subscribe_err: list[str] = []

        def _on_event(ev: dict) -> bool:
            collected.append(ev)
            return True     # 一直收

        def _subscribe():
            try:
                dp.stream({"op": "stream", "timeout": 120, "idleTimeout": 120},
                          host="127.0.0.1", port=DISPATCH_PORT, token=token,
                          timeout=125, on_event=_on_event)
            except Exception as e:      # noqa: BLE001
                subscribe_err.append(f"{type(e).__name__}: {e}")

        sub = threading.Thread(target=_subscribe, daemon=True)
        sub.start()
        time.sleep(1.2)      # 让订阅端先就位（服务端先订阅再回放，顺序不能反）

        ask = dp.call(dp.make_request(
            "send", question="统计一下我和张三的聊天总量，我发出多少条？"),
            host="127.0.0.1", port=DISPATCH_PORT, token=token)
        check(results, "派活 send 被受理", bool(ask.get("ok")),
              str(ask.get("error") or {}))
        run_id = (ask.get("result") or {}).get("runId")
        check(results, "send 返回 runId", bool(run_id), str(run_id))

        # 等这轮跑完（订阅端在旁路收事件）
        if run_id:
            for _ in range(180):
                time.sleep(1.0)
                w = dp.call(dp.make_request("wait", run_id=run_id, timeout=5),
                            host="127.0.0.1", port=DISPATCH_PORT, token=token)
                st = (w.get("result") or {}).get("status")
                if st in ("done", "error", "cancelled"):
                    break
        time.sleep(1.5)      # 给订阅端一点时间把最后几条事件收完

        check(results, "订阅端没有报错", not subscribe_err,
              "; ".join(subscribe_err))
        check(results, "订阅端实时收到事件", len(collected) > 0,
              f"{len(collected)} 条")
        if collected:
            seqs = [e.get("seq") for e in collected]
            check(results, "事件带全局单调 seq",
                  all(isinstance(s, int) for s in seqs) and seqs == sorted(seqs),
                  str(seqs[:8]))
            kinds = {e.get("kind") for e in collected}
            check(results, "事件 kind 合法",
                  all(k in ("runStarted", "toolStarted", "toolFinished",
                            "assistantDelta", "status", "steer",
                            "runCompleted", "runFailed", "runCancelled")
                      for k in kinds), str(kinds))
            check(results, "收到 runStarted",
                  any(e.get("kind") == "runStarted" for e in collected),
                  str(sorted(kinds)))
            check(results, "收到工具轨迹事件",
                  any(e.get("kind") == "toolFinished" for e in collected),
                  f"toolFinished x{sum(1 for e in collected if e.get('kind') == 'toolFinished')}")
            check(results, "收到 runCompleted",
                  any(e.get("kind") == "runCompleted" for e in collected),
                  str(sorted(kinds)))
            # 事件必须属于这一轮
            check(results, "事件带 runId 且属于本轮",
                  all(e.get("runId") == run_id for e in collected
                      if e.get("runId")),
                  str({e.get("runId") for e in collected}))

        # ---------------------------------------------------------- 场景 C
        print("\n[3] 场景 C —— since 游标补历史，不重复")
        if len(collected) >= 2:
            mid = collected[len(collected) // 2]["seq"]
            _h, ev_c = raw_stream({"op": "stream", "since": mid, "timeout": 3},
                                  token, DISPATCH_PORT, timeout=8)
            check(results, "since 游标过滤掉旧事件",
                  all(e["seq"] > mid for e in ev_c),
                  f"since={mid} 收到 {[e['seq'] for e in ev_c][:6]}")
            check(results, "since 游标不重复旧事件",
                  len(ev_c) <= len(collected),
                  f"since 后 {len(ev_c)} 条 vs 全量 {len(collected)} 条")
        else:
            check(results, "since 游标过滤掉旧事件", False, "事件不够，跳过")

        # ---------------------------------------------------------- 场景 D
        print("\n[4] 场景 D —— tail 拿答案")
        if run_id:
            t2 = dp.call(dp.make_request("tail", run_id=run_id, n=50),
                         host="127.0.0.1", port=DISPATCH_PORT, token=token)
            check(results, "tail 调用成功", bool(t2.get("ok")),
                  str(t2.get("error") or {}))
            t2r = t2.get("result") or {}
            check(results, "tail 有 lastAssistant",
                  bool(t2r.get("lastAssistant")),
                  (t2r.get("lastAssistant") or "")[:80])
            check(results, "tail 有消息列表",
                  len(t2r.get("messages") or []) > 0,
                  f"{len(t2r.get('messages') or [])} 条")
            check(results, "tail 的 lastAssistant 非空",
                  len(t2r.get("lastAssistant") or "") > 20,
                  f"{len(t2r.get('lastAssistant') or '')} 字")
        else:
            check(results, "tail 调用成功", False, "没有 runId")

        # ---------------------------------------------------------- 场景 E
        print("\n[5] 场景 E —— stop 真打断正在跑的那轮")
        resp3 = dp.call(dp.make_request("send", question="详细分析我和张三过去一年的所有聊天记录"),
                        host="127.0.0.1", port=DISPATCH_PORT, token=token)
        check(results, "为取消再派一个活", bool(resp3.get("ok")),
              str(resp3.get("error") or {}))
        rid3 = (resp3.get("result") or {}).get("runId")
        if rid3:
            time.sleep(3)     # 让它真的跑起来
            st3 = dp.call(dp.make_request("status"),
                          host="127.0.0.1", port=DISPATCH_PORT, token=token)
            check(results, "取消前状态是 busy",
                  bool((st3.get("result") or {}).get("busy")),
                  str((st3.get("result") or {}).get("busy")))
            sp = dp.call(dp.make_request("stop", run_id=rid3,
                                         reason="端到端测试取消"),
                         host="127.0.0.1", port=DISPATCH_PORT, token=token)
            check(results, "stop 被受理", bool(sp.get("ok")),
                  str(sp.get("error") or {}))
            spr = sp.get("result") or {}
            check(results, "stop 报告已停止", bool(spr.get("stopped")),
                  str(spr.get("note")))
            # wait 必须能看到 cancelled（不能干等到超时）
            w3 = dp.call(dp.make_request("wait", run_id=rid3, timeout=30),
                         host="127.0.0.1", port=DISPATCH_PORT, token=token)
            w3r = w3.get("result") or {}
            check(results, "wait 看到 cancelled",
                  w3r.get("status") == "cancelled", str(w3r.get("status")))
            check(results, "取消后答案不是完整答案",
                  bool(w3r.get("cancelled")),
                  str(w3r.get("cancelled")))

        # ---------------------------------------------------------- 场景 F
        print("\n[6] 场景 F —— 停不到的时候不能说停了")
        # 没有在跑 → NOT_CANCELLABLE
        sp2 = dp.call(dp.make_request("stop"),
                      host="127.0.0.1", port=DISPATCH_PORT, token=token)
        check(results, "空闲时 stop 明确报 NOT_CANCELLABLE",
              (sp2.get("error") or {}).get("code") == "NOT_CANCELLABLE",
              str(sp2.get("error") or {}))

        # ---------------------------------------------------------- 场景 G
        print("\n[7] 场景 G —— 鉴权与脱敏")
        # token 不对 → 拒绝
        try:
            with socket.create_connection(("127.0.0.1", DISPATCH_PORT),
                                          timeout=8) as s:
                raw = json.dumps({"op": "stream", "timeout": 2}).encode("utf-8")
                head = (f"POST /v1 HTTP/1.1\r\nHost: 127.0.0.1:{DISPATCH_PORT}\r\n"
                        f"Authorization: Bearer {'0' * 64}\r\n"
                        f"Content-Length: {len(raw)}\r\n"
                        f"Connection: close\r\n\r\n").encode("ascii")
                s.sendall(head + raw)
                resp_b = s.recv(65536)
            check(results, "token 不对 → 401", b"401" in resp_b.split(b"\r\n")[0],
                  resp_b.split(b"\r\n")[0].decode("latin-1"))
        except Exception as e:
            check(results, "token 不对 → 401", False, f"{type(e).__name__}: {e}")

        # 脱敏：往日志塞一条带 key 的事件，流里不能出现明文
        # （用真实的服务端做不到塞事件，所以验协议层的脱敏已由单测覆盖；
        #  这里验「流里不会凭空出现 API key」——即流里没有配置里的真 key）
        _h, ev_g = raw_stream({"op": "stream", "timeout": 3},
                              token, DISPATCH_PORT, timeout=8)
        dumped = json.dumps(ev_g, ensure_ascii=False)
        real_key = env_cfg.get("MOSU_BOT_API_KEY") or ""
        if real_key:
            leaked = real_key in dumped
            check(results, "事件流里不出现真 API Key", not leaked,
                  "密钥泄漏到事件流里了" if leaked else
                  f"已扫 {len(dumped)} 字符，未出现明文 key")
        else:
            check(results, "事件流里不出现真 API Key", True, "（本机没配 key，跳过）")

        # ---------------------------------------------------------- 场景 H
        print("\n[8] 场景 H —— GUI 侧的洞察也进同一条事件流")
        # GUI 才是墨溯的主入口。如果用户在 GUI 里问的问题
        # 外部订阅端看不到，stream 这个能力就只在边角场景可用。
        gui_events: list[dict] = []
        gui_err: list[str] = []

        def _gui_subscribe():
            try:
                dp.stream({"op": "stream", "timeout": 150, "idleTimeout": 150},
                          host="127.0.0.1", port=DISPATCH_PORT, token=token,
                          timeout=155,
                          on_event=lambda ev: gui_events.append(ev) or True)
            except Exception as e:      # noqa: BLE001
                gui_err.append(f"{type(e).__name__}: {e}")

        gsub = threading.Thread(target=_gui_subscribe, daemon=True)
        gsub.start()
        time.sleep(1.2)

        g_ask = http("/api/insight/ask",
                     {"question": "我最近一个月最常聊的人是谁？"},
                     timeout=30)
        g_tid = g_ask.get("taskId")
        check(results, "GUI 洞察任务已提交", bool(g_tid), str(g_tid))

        g_done = False
        for _ in range(200):
            time.sleep(1.0)
            t = http("/api/task?id=" + g_tid, timeout=20) if g_tid else {}
            if t.get("status") in ("done", "error"):
                g_done = True
                break
        check(results, "GUI 洞察跑完", g_done)
        time.sleep(1.5)      # 等订阅端收完尾部事件

        check(results, "GUI 侧订阅端没有报错", not gui_err,
              "; ".join(gui_err))
        gui_run_ids = {e.get("runId") for e in gui_events if e.get("runId")}
        check(results, "订阅端看到了 GUI 那一轮的事件",
              len(gui_events) > 0,
              f"{len(gui_events)} 条，runId={sorted(gui_run_ids)}")
        if gui_events:
            check(results, "GUI 轮次的事件带 runStarted",
                  any(e.get("kind") == "runStarted" for e in gui_events),
                  str(sorted({e.get("kind") for e in gui_events})))
            check(results, "GUI 轮次的事件带工具轨迹",
                  any(e.get("kind") == "toolFinished" for e in gui_events),
                  f"toolFinished x{sum(1 for e in gui_events if e.get('kind') == 'toolFinished')}")

        # GUI 的轮次也要能被 wait/tail 按 runId 查到
        if g_done and gui_run_ids:
            gid = sorted(gui_run_ids)[-1]
            gw = dp.call(dp.make_request("wait", run_id=gid, timeout=5),
                         host="127.0.0.1", port=DISPATCH_PORT, token=token)
            gwr = gw.get("result") or {}
            check(results, "wait 能查到 GUI 那一轮",
                  gwr.get("status") == "done", str(gwr.get("status")))
            gt = dp.call(dp.make_request("tail", run_id=gid, n=30),
                         host="127.0.0.1", port=DISPATCH_PORT, token=token)
            gtr = gt.get("result") or {}
            check(results, "tail 能拿到 GUI 那轮的答案",
                  len(gtr.get("lastAssistant") or "") > 20,
                  f"{len(gtr.get('lastAssistant') or '')} 字")

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
    print(f"迭代 9 端到端：{passed}/{total} 通过")
    failed = [n for n, ok in results if not ok]
    if failed:
        print("失败项：")
        for n in failed:
            print("  ❌", n)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
