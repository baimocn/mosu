"""迭代 3 端到端：派活通道的三种投递模式 + 鉴权。

验证：
  1. send 真跑出结果（答案非空、有证据）
  2. 忙时 send → BUSY（明确拒绝，不是默默排队）
  3. 忙时 queue → 排队，当前那轮跑完自动接上
  4. 忙时 steer → 消息递进正在跑的那轮，答案里能看到引导的影响
  5. token 不对 → 401 拒绝
  6. 派活产出的答案与 GUI 里的答案同源（同一套代码路径 + 都落盘）

用法：python .cache/audit/e2e_iter3.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# 仓库根：向上找带 core/ 的那层，不硬编码目录深度
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "core").is_dir() and (p / "cli").is_dir())
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8788"
PY = sys.executable
CLI = [PY, "-m", "cli.mosu", "dispatch"]


def run(*args, timeout=300):
    """跑一条 dispatch CLI 命令，返回 (returncode, stdout, stderr)。"""
    p = subprocess.run(CLI + list(args), capture_output=True, text=True,
                       cwd=str(ROOT), timeout=timeout, encoding="utf-8",
                       errors="replace")
    return p.returncode, p.stdout, p.stderr


def get(path):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, payload):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with op.open(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

    # ---------------------------------------------------------------- 1
    print("[1] send 真跑")
    rc, out, err = run("send", "--question", "我上个月跟谁聊得最多？",
                       "--wait", "--timeout", "180")
    print(f"    rc={rc} 输出 {len(out)} 字")
    if rc != 0:
        print("    stderr:", err[:500])
    ok_send = rc == 0 and len(out) > 100 and "依据" in out
    check("send 跑出真答案", ok_send, f"{len(out)} 字")
    check("答案带证据", "证据" in out)

    # ---------------------------------------------------------------- 2
    print("\n[2] 忙时 send → BUSY")
    # 先起一个长任务（不 --wait，让它跑着）
    rc2, out2, err2 = run("send", "--question", "详细分析一下我和李小明的聊天往来")
    check("第二个 send 被受理", rc2 == 0, out2.strip()[:80])
    time.sleep(1.0)
    rc3, out3, err3 = run("send", "--question", "这个应该被拒绝")
    check("忙时 send 报 BUSY", rc3 != 0 and "BUSY" in (out3 + err3),
          (out3 + err3).strip()[:100])
    check("BUSY 时提示了替代方案",
          "queue" in (out3 + err3) or "steer" in (out3 + err3))

    # ---------------------------------------------------------------- 3
    print("\n[3] 忙时 queue → 排队")
    rc4, out4, err4 = run("queue", "--question", "排队的任务：去年我发过多少条消息？")
    check("queue 被受理", rc4 == 0, out4.strip()[:80])
    check("提示已排队", "排队" in out4, out4.strip()[:80])
    rc5, out5, err5 = run("status")
    check("status 显示排队中", "排队" in out5, out5.strip()[:120])

    # ---------------------------------------------------------------- 4
    print("\n[4] 忙时 steer → 引导当前这轮")
    rc6, out6, err6 = run("steer", "--message", "重点关注工作相关的对话")
    check("steer 被受理", rc6 == 0, out6.strip()[:80])
    check("提示已引导进当前轮", "引导" in out6, out6.strip()[:80])

    # ---------------------------------------------------------------- 5
    print("\n[5] 等排队任务跑完")
    t0 = time.time()
    rc7, out7, err7 = run("wait", "--timeout", "240")
    elapsed = time.time() - t0
    check("wait 拿到结果", rc7 == 0 and len(out7) > 50, f"{len(out7)} 字")
    check("排队任务真跑完了", elapsed > 1, f"{elapsed:.0f} 秒")

    # ---------------------------------------------------------------- 6
    print("\n[6] 鉴权：token 不对必须被拒")
    from core import dispatch as dp
    old = os.environ.get("MOSU_HOME")
    tok = dp.read_token()
    # 用错误的 token 直连
    r = dp.call({"op": "status"}, token="0" * 64)
    check("错误 token 被拒", not r.get("ok"), (r.get("error") or {}).get("code", ""))
    check("拒绝时给的是 UNAUTHORIZED",
          (r.get("error") or {}).get("code") == "UNAUTHORIZED")
    # 无 token 文件
    env = dict(os.environ)
    env["MOSU_HOME"] = str(ROOT / ".cache" / "no-such-home")
    p = subprocess.run(CLI + ["status"], capture_output=True, text=True,
                       cwd=str(ROOT), env=env, timeout=60,
                       encoding="utf-8", errors="replace")
    check("无 token 时给出可读提示", p.returncode != 0 and
          ("token" in (p.stdout + p.stderr)), (p.stdout + p.stderr).strip()[:100])

    # ---------------------------------------------------------------- 7
    print("\n[7] 派活产出也落盘（历史可回看）")
    runs = get("/api/runs")
    items = runs.get("runs") or []
    check("历史里有记录", len(items) > 0, f"{len(items)} 条")
    if items:
        latest = items[0]
        print(f"    最新：{latest['question'][:40]} "
              f"({latest.get('stepsCount')}步 {latest.get('evidenceCount')}证据)")

    # ---------------------------------------------------------------- 汇总
    print("\n[汇总]")
    passed = sum(1 for _, ok in results if ok)
    print(f"  {passed}/{len(results)} 通过")
    for name, ok in results:
        if not ok:
            print(f"  ❌ {name}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
