"""迭代 2 端到端验证：真模型 + 真数据 + 真 SSE。

验证四件事：
  1. SSE 能实时推事件（不是等结果出来才一次性给）——**首事件延迟**是硬指标
  2. 事件顺序正确（snapshot 先到，再是 delta 增量，最后 done）
  3. 运行结束后 ~/.mosu/runs/ 里真的有记录，能读回来
  4. 历史列表接口能列出来，详情接口能读全文

用法：python .cache/audit/e2e_iter2.py
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# 仓库根：向上找带 core/ 的那层，不硬编码目录深度
ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "core").is_dir() and (p / "cli").is_dir())
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8788"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def read_sse(tid, timeout=180):
    """读 SSE 流，返回 (事件列表, 首事件延迟秒, 各事件到达时间)。

    自己解析 SSE（不引第三方库）：EventSource 的格式是
    `event: <名字>\\ndata: <json>\\n\\n`。
    """
    url = f"{BASE}/api/insight/stream?id={tid}"
    events = []
    t0 = time.time()
    first_delay = None
    buf = b""
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        while True:
            chunk = r.read1(4096) if hasattr(r, "read1") else r.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                text = raw.decode("utf-8", "replace").strip()
                if not text or text.startswith(":"):
                    continue          # 心跳注释行
                ev = {"name": "message", "data": {}}
                for line in text.split("\n"):
                    if line.startswith("event: "):
                        ev["name"] = line[7:].strip()
                    elif line.startswith("data: "):
                        try:
                            ev["data"] = json.loads(line[6:])
                        except ValueError:
                            ev["data"] = line[6:]
                now = time.time() - t0
                if first_delay is None:
                    first_delay = now
                ev["at"] = round(now, 2)
                events.append(ev)
                if ev["name"] in ("done", "error"):
                    return events, first_delay, t0
    return events, first_delay, t0


def main():
    q = "我最近一次跟李小明聊天是什么时候，聊了什么？"
    print(f"[提问] {q}\n")

    t0 = time.time()
    r = post("/api/insight/ask", {"question": q})
    tid = r["taskId"]
    print(f"[任务] taskId={tid}")

    events, first_delay, _ = read_sse(tid)
    total = time.time() - t0

    print(f"\n[SSE] 共收到 {len(events)} 个事件")
    for e in events[:60]:
        d = e["data"]
        extra = ""
        if e["name"] == "delta":
            extra = f" kind={d.get('kind')}"
            if d.get("kind") == "step":
                st = d.get("step") or {}
                extra += f" [{st.get('index')}] {st.get('label')}"
        elif e["name"] == "snapshot":
            extra = f" status={d.get('status')} steps={len((d.get('live') or {}).get('steps') or [])}"
        elif e["name"] == "done":
            res = d.get("result") or {}
            extra = f" answer={len(res.get('answer') or '')}字 evidence={len(res.get('evidence') or [])}"
        print(f"   {e['at']:>6.2f}s  {e['name']:<9}{extra}")

    # ---- 判定 ----
    print("\n[判定]")
    names = [e["name"] for e in events]
    ok_snapshot = names and names[0] == "snapshot"
    print(f"  首事件是 snapshot：{'✅' if ok_snapshot else '❌ ' + str(names[:3])}")

    deltas = [e for e in events if e["name"] == "delta"]
    step_deltas = [e for e in deltas if (e["data"].get("kind")) == "step"]
    print(f"  收到 step 增量事件：{'✅' if step_deltas else '❌'}（{len(step_deltas)} 条）")

    has_done = "done" in names
    print(f"  收到 done 事件：{'✅' if has_done else '❌'}")

    # 实时性硬指标：首事件必须在 1 秒内到达（说明是真推送，不是等结果）
    # 若退化成轮询式，首事件会等到整个分析结束（十几秒）
    realtime = first_delay is not None and first_delay < 1.0
    print(f"  首事件延迟 {first_delay:.2f}s < 1s（真推送而非事后补发）："
          f"{'✅' if realtime else '❌'}")

    # step 增量应该在 done 之前陆续到达，而不是一次性全在最后
    if step_deltas and has_done:
        done_at = [e["at"] for e in events if e["name"] == "done"][0]
        spread = step_deltas[-1]["at"] - step_deltas[0]["at"]
        print(f"  step 事件分布在 {step_deltas[0]['at']:.2f}s~{step_deltas[-1]['at']:.2f}s"
              f"（done 在 {done_at:.2f}s）：{'✅ 增量陆续到达' if spread > 0.5 else '❌ 疑似一次性补发'}")

    # ---- 落盘验证 ----
    print("\n[落盘]")
    runs = get("/api/runs")
    items = runs.get("runs") or []
    print(f"  历史列表条数：{len(items)}")
    if items:
        top = items[0]
        print(f"  最新一条：{top['question'][:40]}")
        print(f"    steps={top.get('stepsCount')} evidence={top.get('evidenceCount')} "
              f"branches={top.get('branchesCount')} cost={top.get('cost')}")
        detail = get(f"/api/runs/detail?id={top['id']}")
        ans = detail.get("answer") or ""
        ev = detail.get("evidence") or []
        st = detail.get("steps") or []
        print(f"  详情读回：答案 {len(ans)} 字，证据 {len(ev)} 条，步骤 {len(st)} 步")
        ok_detail = len(ans) > 50 and len(st) > 0
        print(f"  详情可完整读回：{'✅' if ok_detail else '❌'}")
        # 内容一致性：落盘的答案必须跟 SSE done 里的一致
        done_res = [e["data"].get("result") or {} for e in events if e["name"] == "done"]
        if done_res:
            same = (done_res[0].get("answer") or "") == ans
            print(f"  落盘答案与 SSE 结果一致：{'✅' if same else '❌'}")
    else:
        print("  ❌ 历史列表为空——落盘没生效")
        ok_detail = False

    print(f"\n[总耗时] {total:.1f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
