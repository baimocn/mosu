# -*- coding: utf-8 -*-
"""洞察流程契约 —— 给 Agentic 循环补一层「事后机械核验」。

为什么要有这个
--------------
对标 Lancer 的 `packages/video-learn-process/PROCESS.json`。
读完那份契约才发现：Lancer 的「阶段流水线」**不是视频功能**，
而是一份**声明式流程契约**，真正值得抄的是它的骨架：

1. **status code 带机器可判的推进条件**（`ok_to_proceed`）
   —— 状态不只是给人看的标签，而是「下游能不能继续」的判据。
2. **每阶段一个 gate 产物**（`_gates/<step>.<id>.json`，绑 sha256）
   —— 阶段的产出要落成可核验的文件，不是留在上下文里靠模型自己记。
3. **forbidden 清单** —— 明确列出「不许做的事」，
   Lancer 原话：`prose_handoff_messages`、`generic_prompt_compression_as_handoff`
   —— 它反对的正是「用散文把阶段串起来」。
4. **每阶段一行机械摘要** —— Lancer 注释原文：
   *"One short line of ids, paths, hashes, and status codes.
   No narrative and no media bytes."*

墨溯怎么抄
----------
**不抄固定管道。** 迭代 2/3 的教训：墨溯原来就是固定管道
（预塞 7 条事实 + 8 条语料 → 模型一次调用），
用户批「ai 功能好差劲」——模型被绑住了手，
问「我和某人上个月聊了什么」它压根看不到那份记录。

所以墨溯抄的是**契约层**，不是**编排层**：

- 模型**照旧自主**选工具、决定查几步、什么时候收尾（ReAct 循环不变）；
- 我们只做**事后判定**：把已经发生的工具调用按契约归类到阶段，
  再按契约算出每个阶段的状态码和 gate。

换句话说：Lancer 是「先定流水线再跑」，
墨溯是「先跑完再按流水线验收」。**顺序反了，但验收标准一样严。**

本模块只做判定，不碰模型、不改答案、不发起任何查询。
纯标准库，零依赖。

判定纪律
--------
契约是**验收标准**，不是**行为指导**。所以：

- 判定只依据**已经发生的事实**（工具调用记录、证据池、答案文本）；
- 任何判定不得依赖模型「应该怎么做」；
- 判定结果必须能追到具体证据（哪个工具、哪条 evidence、哪个数字）。
"""
from __future__ import annotations

import re
from typing import Any

# ------------------------------------------------------------------ 阶段定义

# 洞察流程的四个阶段。
#
# 分阶段依据是「这一步在回答里承担什么角色」，不是 Lancer 的视频步骤
# （下载 → 媒体探测 → 视觉分析 → 复刻）：
#   scope   —— 锁定问的是谁、哪段时间（范围认定）
#   probe   —— 拿到数据本身（统计/检索）
#   read    —— 读到原文（引语必须来自这里，不是从统计里脑补）
#   verify  —— 核对答案里的数字有没有出处
#
# actor 用 Lancer 的同一套取值（human / local / gateway / codex），
# 这样两边能对照着看。
STAGES: tuple[dict[str, Any], ...] = (
    {
        "id": "scope",
        "label": "范围认定",
        "actor": "local",
        "goal": "确认这次问的是谁、哪段时间",
        # 没有这一步的阶段状态只能是 skipped（不阻断）
        "required": False,
        # source=tool：这个阶段由工具调用产生
        # source=model：这个阶段由模型产生（成文）
        "source": "tool",
    },
    {
        "id": "probe",
        "label": "数据探测",
        "actor": "local",
        "goal": "取到统计与检索结果",
        "required": True,
        "source": "tool",
    },
    {
        "id": "read",
        "label": "原文取证",
        "actor": "local",
        "goal": "读到消息原文，供引语落地",
        "required": False,
        "source": "tool",
    },
    {
        "id": "verify",
        "label": "答案核验",
        "actor": "local",
        "goal": "核对答案里的数字有没有出处",
        "required": True,
        "source": "tool",
    },
    {
        # 成文阶段：唯一一个 source=model 的阶段。
        #
        # 为什么加它：Lancer 的 handoff 边谓词 `distinct_configured_model`
        # 要求**交付阶段用与上游不同的模型**。墨溯没多步流水线，
        # 但有同一个盲区——取证和下结论是一个模型，它自己查的数据
        # 自己确认，自己没法质疑自己。这个阶段把「成文」拆出来，
        # 让成文模型只看证据（不看取证者的自述）独立下结论。
        "id": "compose",
        "label": "答案成文",
        "actor": "local",
        "goal": "由成文模型基于证据独立写出答案",
        "required": False,
        "source": "model",
    },
)

# 工具 → 阶段的归类。
#
# 归类依据是工具**返回什么形态的证据**，不是工具名字好不好听：
#   - 只给结构化计数/分布 → probe
#   - 能给出消息原文（带 sender + 文本）→ read
#   - 解决「是谁/哪段时间」这类指代 → scope
TOOL_STAGE: dict[str, str] = {
    "resolve_time": "scope",
    "resolve_name": "scope",
    "find_contact": "scope",
    "list_sessions": "scope",
    "chat_stats": "probe",
    "search_messages": "read",
    "read_timeline": "read",
    "session_detail": "probe",
    "my_profile": "probe",
    "search_moments": "read",
    # 委派是编排动作，不产证据，单独归类（不计入任何数据阶段）
    "delegate_tasks": "orchestrate",
}

# 能产出「原话」的工具（kind=quote）。
# 统计类证据（kind=stat）**不算**原话——
# 墨溯的既有纪律：不能拿统计数字冒充某人说过的话。
QUOTE_TOOLS = ("search_messages", "read_timeline", "search_moments")


# ------------------------------------------------------------------ 状态码

# 每个状态码带 ok_to_proceed —— Lancer 的核心机制：
# 状态不只是标签，而是**机器可判的推进条件**。
#
# 墨溯的语义：ok_to_proceed=False 表示「这一阶段的产出不足以支撑
# 一个可信的答案」，前端要挂提醒，CLI 要标警告。
STATUS_CODES: dict[str, dict[str, Any]] = {
    "OK": {
        "label": "通过",
        "ok_to_proceed": True,
        "hint": "",
    },
    # 阶段没跑（这类问题本来就不需要它）——不算失败
    "SKIPPED": {
        "label": "未涉及",
        "ok_to_proceed": True,
        "hint": "",
    },
    # 跑了但产出为空
    "EMPTY": {
        "label": "查无数据",
        "ok_to_proceed": False,
        "hint": "这一阶段执行了但没取到任何东西，结论可能只覆盖了部分数据。",
    },
    # 关键阶段没跑
    "MISSING": {
        "label": "未执行",
        "ok_to_proceed": False,
        "hint": "关键阶段没有执行，答案缺少这一环节的支撑。",
    },
    # 有数据但没有原话支撑（引语无处落地）
    "NO_QUOTE": {
        "label": "缺原话支撑",
        "ok_to_proceed": False,
        "hint": "结论引用了具体话语，但没有对应的消息原文可核对。",
    },
    # 数字对不上出处
    "UNSOURCED_NUMBER": {
        "label": "数字无出处",
        "ok_to_proceed": False,
        "hint": "答案里有数字在工具返回里找不到出处，需人工复核。",
    },
    # 委派失败
    "DELEGATE_FAILED": {
        "label": "委派失败",
        "ok_to_proceed": True,
        "hint": "并行子任务没跑成，主代理改用自己查——不影响结论。",
    },
    # 成文模型调用失败（已退回取证模型自己写的答案）
    "COMPOSE_FAILED": {
        "label": "成文失败",
        "ok_to_proceed": True,
        "hint": "成文模型没跑通，已退回取证模型自己写的答案——交叉核验这一轮没做成。",
    },
}


# ------------------------------------------------------------------ 边谓词

# Lancer 的 `hard_edges` / `optional_edges` 里每条边都带**谓词**，
# 比如 handoff 那条：`verified_spec_sha256 && verified_frame_references &&
# distinct_configured_model`。
#
# 墨溯对应的那条谓词就是 `distinct_configured_model`：
# **成文用的模型必须与取证用的模型不同**——否则等于自己审自己的卷。
# 谓词不满足时，optional edge 不触发（跟 Lancer 一样：默认跳过，不阻断）。
PREDICATES: tuple[dict[str, str], ...] = (
    {
        "id": "distinct_configured_model",
        "text": "成文模型必须与取证模型不同",
        "why": "同一个模型查的数据自己确认、自己写结论，等于自己给自己签字。",
    },
)


# ------------------------------------------------------------------ 禁止清单

# Lancer 的 forbidden 列表抄过来，按墨溯的语境改写。
#
# 墨溯版每条都**可程序化检测**——不能检测的条目不写进来
# （Lancer 那份有几条是给人看的约定，墨溯只留能自动判的）。
FORBIDDEN: tuple[dict[str, str], ...] = (
    {
        "id": "no_number_without_source",
        "text": "答案里的数字必须有工具返回做出处",
        "why": "模型会自己把 2814 改成 2904，用户按 2904 核对怎么都对不上。",
    },
    {
        "id": "no_stat_as_quote",
        "text": "统计口径不能当某人说过的话",
        "why": "「消息量 158565 条」是数据库算的，不是谁的原话，混淆两者就是撒谎。",
    },
    {
        "id": "no_answer_without_evidence",
        "text": "答案必须有证据支撑，不能空手作答",
        "why": "没有任何工具调用就给结论，等于编。",
    },
    {
        "id": "no_quote_without_read",
        "text": "引用具体话语前必须读过原文",
        "why": "只拿到统计就脑补「他当时说」，检索只给线索，不给定论。",
    },
    {
        "id": "no_self_review",
        "text": "成文模型不能与取证模型是同一个",
        "why": "自己查的数据自己确认、自己写结论，等于自己给自己签字——"
               "这正是 Lancer 的 distinct_configured_model 谓词要防的事。",
    },
    {
        "id": "no_stat_without_scope",
        "text": "数字证据必须带口径（统计对象 / 时间范围 / 统计维度）",
        "why": "「消息量 158565 条」这句话本身没错，但用户没法核对——"
               "是全库还是某个会话？是哪段时间？没有口径的数字，"
               "用户只能选择信或不信，而墨溯的整个卖点就是每句话都能核对。",
    },
)


# ------------------------------------------------------------------ 判定

def _stage_index() -> dict[str, int]:
    return {s["id"]: i for i, s in enumerate(STAGES)}


def _status(name: str, detail: str = "") -> dict[str, Any]:
    """造一个状态对象（字段名与 Lancer 的 stage 结构对齐）。"""
    spec = STATUS_CODES.get(name, STATUS_CODES["MISSING"])
    return {
        "code": name,
        "label": spec["label"],
        "okToProceed": spec["ok_to_proceed"],
        "hint": spec["hint"],
        "detail": detail,
    }


def _mechanical_summary(stage_id: str, status: str, facts: dict[str, Any]) -> str:
    """一行机械摘要。

    抄 Lancer 的原注释：*"One short line of ids, paths, hashes, and
    status codes. No narrative and no media bytes."*

    只放标识、数量、状态码、模型名——**不放叙述**。
    叙述由模型的 summary 字段负责，这里不重复。
    """
    bits = [f"stage={stage_id}", f"status={status}"]
    for key in ("calls", "evidence", "quotes", "numbers", "unsourced",
                "collectModel", "writeModel", "model", "distinct"):
        if key in facts:
            bits.append(f"{key}={facts[key]}")
    return " ".join(bits)


def classify_steps(steps: list[dict]) -> dict[str, list[dict]]:
    """把工具调用轨迹按阶段归类。

    返回 {stage_id: [step, ...]}。
    没有 stage_id 的归到 "other"（不丢数据，只是没归类）。
    """
    out: dict[str, list[dict]] = {}
    for st in steps or []:
        tool = st.get("tool") or ""
        stage = TOOL_STAGE.get(tool, "other")
        out.setdefault(stage, []).append(st)
    return out


def _has_call(steps: list[dict], tool: str) -> bool:
    return any(s.get("tool") == tool for s in steps or [])


def evaluate(run: dict) -> dict[str, Any]:
    """对一次已完成的洞察运行做阶段契约判定。

    参数 run：至少包含这些键（缺失按空处理）：
        steps    —— 工具调用轨迹 [{tool, ok, ...}]
        evidence —— 证据池 [{kind, text, ...}]
        answer   —— 最终答案文本
        toolNumbers —— 工具返回里出现过的数字集合（字符串形式）
        branches —— 子代理分支记录（判委派成败）

    返回：
        {
          "stages": [{id, label, actor, goal, status:{...}, summary, required}],
          "gates":  [{stage, status, summary, ...}],   # 每阶段一个 gate 产物
          "forbidden": [{id, text, why, violated, detail}],
          "okToProceed": bool,     # 全部阶段都过 → True
          "checks": [...],          # 逐条禁止项的判定明细
        }
    """
    steps = run.get("steps") or []
    evidence = run.get("evidence") or []
    answer = run.get("answer") or ""
    tool_numbers = run.get("toolNumbers") or set()
    branches = run.get("branches") or []

    by_stage = classify_steps(steps)

    # ---- 阶段判定 --------------------------------------------------
    stage_states: dict[str, dict[str, Any]] = {}

    # scope：有没有做过范围认定
    scope_calls = by_stage.get("scope") or []
    scope_state = _status("OK" if scope_calls else "SKIPPED")
    stage_states["scope"] = {
        "status": scope_state,
        "facts": {"calls": len(scope_calls)},
    }

    # probe：关键阶段。跑了但一条数据都没有 → EMPTY
    probe_calls = by_stage.get("probe") or []
    probe_ev = [e for e in evidence if e.get("kind") == "stat"]
    if not probe_calls:
        probe_state = _status("MISSING")
    elif not probe_ev:
        probe_state = _status("EMPTY")
    else:
        probe_state = _status("OK")
    stage_states["probe"] = {
        "status": probe_state,
        "facts": {"calls": len(probe_calls), "evidence": len(probe_ev)},
    }

    # read：非关键，但「引用了具体话语却没有原文」要拦
    read_calls = by_stage.get("read") or []
    quote_ev = [e for e in evidence if e.get("kind") == "quote"]
    if not read_calls:
        read_state = _status("SKIPPED")
    elif not quote_ev:
        read_state = _status("EMPTY")
    else:
        read_state = _status("OK")
    stage_states["read"] = {
        "status": read_state,
        "facts": {"calls": len(read_calls), "quotes": len(quote_ev)},
    }

    # verify：数字出处核对。这是墨溯的**关键**阶段
    # （对应 Lancer 的 analysis.ok —— 前置门控必须成立才能出结论）。
    unsourced = list(run.get("unsourcedNumbers") or [])
    if not answer.strip():
        verify_state = _status("MISSING", "答案为空")
    elif unsourced:
        verify_state = _status("UNSOURCED_NUMBER",
                               "、".join(unsourced[:5]))
    else:
        verify_state = _status("OK")
    numbers = _count_numbers(answer)
    stage_states["verify"] = {
        "status": verify_state,
        "facts": {"numbers": numbers, "unsourced": len(unsourced)},
    }

    # compose：成文阶段（唯一的 source=model 阶段）。
    #
    # 判定依据是「这一轮实际用了哪些模型」——
    # 入参里有一个 modelRouting 键，内容形如：
    #   {"collectModel": "glm-5",         # 取证（跑 ReAct 循环）的模型
    #    "writeModel":   "deepseek-chat", # 成文的模型（没配就是空串）
    #    "distinct":     true,            # 两个模型是否真的不同
    #    "applied":      true}            # 成文是否真的执行了
    #
    # 没配成文模型 → SKIPPED（不阻断，回到迭代 7 的行为）。
    # 配了但与取证模型相同 → 也 SKIPPED，但**要挂 forbidden**：
    #   这是 Lancer 的 distinct_configured_model 谓词要拦的事。
    routing = run.get("modelRouting") or {}
    collect_model = str(routing.get("collectModel") or "")
    write_model = str(routing.get("writeModel") or "")
    distinct = bool(routing.get("distinct"))
    applied = bool(routing.get("applied"))
    if not write_model:
        compose_state = _status("SKIPPED")
        compose_facts = {"collectModel": collect_model or "(未记录)"}
    elif not distinct:
        compose_state = _status("SKIPPED")
        compose_facts = {"collectModel": collect_model or "(未记录)",
                         "writeModel": write_model}
    elif not applied:
        compose_state = _status("COMPOSE_FAILED")
        compose_facts = {"collectModel": collect_model or "(未记录)",
                         "writeModel": write_model, "distinct": distinct}
    else:
        compose_state = _status("OK")
        compose_facts = {"collectModel": collect_model or "(未记录)",
                         "writeModel": write_model, "distinct": distinct}
    stage_states["compose"] = {
        "status": compose_state,
        "facts": compose_facts,
    }

    # ---- 组装阶段列表 + gate 产物 ----------------------------------
    stages_out: list[dict[str, Any]] = []
    gates: list[dict[str, Any]] = []
    for spec in STAGES:
        sid = spec["id"]
        st = stage_states[sid]
        status = st["status"]
        facts = st["facts"]
        summary = _mechanical_summary(sid, status["code"], facts)
        stages_out.append({
            "id": sid,
            "label": spec["label"],
            "actor": spec["actor"],
            "goal": spec["goal"],
            "required": spec["required"],
            "status": status,
            "summary": summary,
        })
        # gate 产物：每阶段一个，绑状态码 + 机械摘要。
        # Lancer 的 gate 是磁盘文件（_gates/<step>.<id>.json）；
        # 墨溯不落独立文件（产物是回答本身，不是文件树），
        # 但**结构对齐**：stage / status_code / summary 三件套齐全。
        gates.append({
            "stage": sid,
            "statusCode": status["code"],
            "okToProceed": status["okToProceed"],
            "summary": summary,
            "facts": facts,
        })

    # ---- 禁止项判定 ------------------------------------------------
    checks = _check_forbidden(run, steps, evidence, answer, tool_numbers)
    forbidden = [{
        "id": c["id"],
        "text": c["text"],
        "why": c["why"],
        "violated": c["violated"],
        "detail": c["detail"],
    } for c in checks]

    ok = all(s["status"]["okToProceed"] for s in stages_out)

    # 模型路由：取证模型 / 成文模型 / 是否不同 / 边谓词是否成立。
    # 前端要显示「这轮答案是谁写的、跟查数据的是不是同一个模型」。
    routing = run.get("modelRouting") or {}
    cross_model = {
        "collectModel": routing.get("collectModel") or "",
        "writeModel": routing.get("writeModel") or "",
        "distinct": bool(routing.get("distinct")),
        "applied": bool(routing.get("applied")),
        "composeError": routing.get("composeError") or "",
        # 边谓词：distinct_configured_model（对标 Lancer handoff 边）
        "predicate": "distinct_configured_model",
        "predicateSatisfied": bool(routing.get("distinct")
                                   and routing.get("applied")),
    }

    return {
        "stages": stages_out,
        "gates": gates,
        "forbidden": forbidden,
        "checks": checks,
        "okToProceed": ok,
        # 模型路由 / 交叉核验
        "crossModel": cross_model,
        # 供 GUI 一行渲染用
        "summary": " | ".join(
            f"{s['id']}:{s['status']['code']}" for s in stages_out),
    }


def _check_forbidden(run: dict, steps: list[dict], evidence: list[dict],
                     answer: str, tool_numbers: set) -> list[dict[str, Any]]:
    """逐条判定 forbidden 清单。每条都要给出可复现的 detail。"""
    checks: list[dict[str, Any]] = []

    def _mk(spec: dict, violated: bool, detail: str = "") -> None:
        checks.append({
            "id": spec["id"],
            "text": spec["text"],
            "why": spec["why"],
            "violated": violated,
            "detail": detail,
        })

    specs = {f["id"]: f for f in FORBIDDEN}

    # ① 数字无出处
    unsourced = list(run.get("unsourcedNumbers") or [])
    _mk(specs["no_number_without_source"], bool(unsourced),
        ("、".join(unsourced[:5]) if unsourced else ""))

    # ② 统计口径当原话
    #    判据：证据池里有 kind=stat（统计口径）却没有 kind=quote（任何原话）
    #    —— 说明这次回答可能只有统计、没有可核对的原话。
    has_stat = any(e.get("kind") == "stat" for e in evidence)
    has_quote = any(e.get("kind") == "quote" for e in evidence)
    _mk(specs["no_stat_as_quote"], has_stat and not has_quote,
        ("只有统计口径、没有原话" if (has_stat and not has_quote) else ""))

    # ③ 空手作答
    called = [s for s in steps if (s.get("tool") or "") != "delegate_tasks"]
    _mk(specs["no_answer_without_evidence"],
        bool(answer.strip()) and not called and not evidence,
        ("没有任何工具调用却给出了答案" if (answer.strip() and not called
                                          and not evidence) else ""))

    # ④ 引用具体话语但没读过原文
    #    判据：答案里出现了引用标记（引号 / 依据：/ 说过），
    #    但证据池里没有一条 kind=quote。
    mentions_quote = bool(re.search(r"[“\"「]|依据[:：]|说过|原话", answer))
    _mk(specs["no_quote_without_read"],
        mentions_quote and not has_quote,
        ("答案提到具体话语但没有原文证据" if (mentions_quote and not has_quote)
         else ""))

    # ⑤ 自审（成文模型 == 取证模型）
    #    对应 Lancer handoff 边的谓词 `distinct_configured_model`。
    #
    #    判据只有一种：**配了成文模型，且它与取证模型同名**。
    #    「没配成文模型」不算违规——那是用户选择单模型模式，
    #    不是「配置错了」。但「配了却和取证模型一样」是白配：
    #    以为自己开了交叉核验，其实没有。必须点破。
    routing = run.get("modelRouting") or {}
    wm = str(routing.get("writeModel") or "")
    cm = str(routing.get("collectModel") or "")
    if wm and cm and wm == cm:
        _mk(specs["no_self_review"], True,
            f"成文模型与取证模型都是 {wm}，不构成交叉核验")
    else:
        _mk(specs["no_self_review"], False, "")

    # ⑥ 数字证据缺口径
    #    判据：证据池里有 kind=stat，但它的 scope 是空的
    #    （scope/period/metric 三个字段至少有一个非空才算有口径）。
    #
    #    为什么这条能机械检测：口径是工具层强制产出的结构化字段，
    #    不是模型写的散文——模型写「我查了最近三个月」没法机械核对，
    #    但「这条 stat 证据有没有带 scope 字段」是一行代码的事。
    bare_stats = [e for e in evidence
                  if e.get("kind") == "stat" and not _has_scope(e)]
    _mk(specs["no_stat_without_scope"], bool(bare_stats),
        (f"{len(bare_stats)} 条数字证据没有口径"
         if bare_stats else ""))

    return checks


def _has_scope(ev: dict) -> bool:
    """这条证据有没有带口径。

    口径字段可能是三种形态（历史兼容）：
      - 新版：ev["scope"] 是 dict，含 scope/period/metric
      - 简版：ev["scope"] 是字符串，非空即可
      - 旧版：没有 scope 字段 → 视为无口径
    """
    sc = ev.get("scope")
    if isinstance(sc, dict):
        return any(str(sc.get(k) or "").strip()
                   for k in ("scope", "period", "metric"))
    if isinstance(sc, str):
        return bool(sc.strip())
    return False


def _count_numbers(text: str) -> int:
    """答案里的数字个数（与 agent_engine._numbers_in 同口径的粗算）。"""
    if not text:
        return 0
    return len(set(re.findall(r"\d+(?:\.\d+)?", text)))
