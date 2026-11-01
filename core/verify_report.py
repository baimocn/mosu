# -*- coding: utf-8 -*-
"""验证报告校验器 —— 6 级证据分级（见 docs/verification.md）。

为什么要有这个
--------------
前四轮迭代里，验证记录散在对话里，没有任何机器能检查的结构。
结果就是：**我自己都不知道哪些没验**。

最典型的例子是迭代 4：端到端断言「隐藏节点后连线全部消失」，
在连线数为 0 时会**空跑通过**（before=0、after=0 也满足 after==0），
把「重算不跟随布局」这个真 bug 完整盖住。
问题不在于没测，在于**不知道自己没测什么**。

所以这里定两条硬规则，用代码锁死：

1. **`not_run` 字段必须存在** —— 哪怕是空数组。
   字段缺失 = 报告不合格。
2. **`level` 不能高于 claims 能支撑的强度** —— 防止虚报证据等级。

另外还检查：
- `not_run` 里不能有「未验证」「可能」「其他场景」这类**不可执行**的空话
- `evidence` 里的路径必须真的存在 —— 证据必须能打开
- `claims` 不能为空 —— 一条都不证明的报告没有意义

纯标准库，零依赖。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

# 证据强度分级（封闭表，不在表内 = 非法）
LEVELS = {
    "L1": 1,   # 静态检查
    "L2": 2,   # 单元测试
    "L3": 3,   # 离线集成（真 I/O，夹具数据）
    "L4": 4,   # 真实数据
    "L5": 5,   # 真机端到端
    "L6": 6,   # 真机全链路（真模型 + 真数据 + 跨进程）
}

# 报告里每条 claim 必须声明它属于哪一级证据。
# 校验器据此判断「声明的 level 是否虚报」。
#
# 这不是「给每条 claim 打分」，而是**声明覆盖范围**：
# 一份报告的 level = 它所有 claims 里**最强**的那一条。
# 如果报告整体声明 L5，但所有 claims 都是 L2，那 L5 就是虚报。
CLAIM_LEVEL_KEY = "level"

# 「没跑」清单里不可接受的空话 —— **按「范围无法界定」判定，不按关键词**。
#
# 第一版用的是关键词黑名单（含「未验」「未验证」），结果误伤了合法条目：
# 「派活任务的取消/中止——协议里没有 cancel 语义，未验」这条**完全可执行**
# （去补 cancel 语义），却因为带「未验」被判空话。
#
# 所以改成两条实质判据：
#   ① **范围无法界定** —— 「其他场景」「若干」「部分」「大概」这类词，
#      读者没法知道要补什么
#   ② **长度过短** —— 一句话说不清补什么
#
# 「未验」「未验证」本身**不是**问题：只要写清「什么」没验，就是合格条目。
VAGUE_MARKERS = (
    "其他场景", "其他情况", "其他环境", "若干", "一些", "部分",
    "大概", "大致", "等等", "暂缺", "待补", "以后再说", "后续补",
    "可能存在", "可能还有", "视情况", "未覆盖全部", "暂未覆盖",
)

# 「没跑」条目的最短长度（去掉空白后）。
# 低于这个长度基本等于没写清楚要补什么。
MIN_NOT_RUN_LEN = 14

# 报告必须有的字段
REQUIRED_FIELDS = ("title", "level", "claims", "not_run", "evidence")

# 「本机证据」前缀。
#
# 为什么需要区分：`.cache/` 在 .gitignore 里（审计脚本、截图都是本地产物），
# 报告本身却要入库。如果一律要求 evidence 存在，
# 别人 clone 仓库后校验自己的报告必然失败。
#
# 所以：**入库路径必须真的存在**（tests/、core/、docs/ …），
# `.cache/` 开头的视为本机证据，只在本机存在时才提示，
# 但**不判不合格**——它记录的是「当时在那台机器上看到了什么」。
LOCAL_EVIDENCE_PREFIXES = (".cache/",)

# 仓库根（本文件在 core/ 下，父目录的父目录）
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class ReportError(Exception):
    """报告不合格。message 里写清楚哪一条不合格、为什么。"""


def _is_vague(text: str) -> str | None:
    """判断一句「没跑」是不是不可执行的空话。

    返回问题描述；合格则返回 None。
    """
    s = (text or "").strip()
    if not s:
        return "条目为空"
    compact = "".join(s.split())
    if len(compact) < MIN_NOT_RUN_LEN:
        return (f"太短，说不清要补什么：{s!r}"
                f"（至少 {MIN_NOT_RUN_LEN} 字，且要写清补什么）")
    for m in VAGUE_MARKERS:
        if m in s:
            return (f"范围无法界定，含空话词「{m}」：{s!r}"
                    "（要具体到能补做的事，例如"
                    "「Windows 缩放 150% 下的曲线端点精度」）")
    return None


def _claim_level(claim: Any) -> str:
    """取一条 claim 声明的证据级别。

    支持两种写法：
      {"text": "...", "level": "L5"}
      "..."            # 纯字符串 → 按 L2 算（最保守）
    """
    if isinstance(claim, dict):
        lv = claim.get(CLAIM_LEVEL_KEY)
        if isinstance(lv, str) and lv.strip().upper() in LEVELS:
            return lv.strip().upper()
        return "L2"
    return "L2"


def _claim_text(claim: Any) -> str:
    if isinstance(claim, dict):
        return str(claim.get("text") or "").strip()
    return str(claim or "").strip()


def validate(data: Any, *, base_dir: pathlib.Path | None = None) -> list[str]:
    """校验一份报告，返回问题列表（空列表 = 通过）。

    base_dir：解析 evidence 相对路径时的基准目录（通常是报告文件所在目录）。
    """
    problems: list[str] = []

    if not isinstance(data, dict):
        return ["报告根必须是对象"]

    # ---- 必填字段
    for f in REQUIRED_FIELDS:
        if f not in data:
            problems.append(f"缺必填字段 `{f}`")
    if problems:
        # 必填字段都缺，后面的检查没法做
        return problems

    # ---- title
    if not str(data.get("title") or "").strip():
        problems.append("`title` 不能为空")

    # ---- level
    level = str(data.get("level") or "").strip().upper()
    if level not in LEVELS:
        problems.append(f"`level` 非法：{data.get('level')!r}"
                        f"（合法值：{', '.join(LEVELS)}）")
        return problems   # level 非法，后面没法比

    # ---- claims：不能为空
    claims = data.get("claims")
    if not isinstance(claims, list) or not claims:
        problems.append("`claims` 必须是非空数组——一条都不证明的报告没有意义")
    else:
        # level 不得高于 claims 里最强的那一条（防虚报）
        strongest = max(LEVELS[_claim_level(c)] for c in claims)
        if LEVELS[level] > strongest:
            problems.append(
                f"虚报证据等级：声明 `{level}`，"
                f"但 claims 里最强的一条只到 `L{strongest}`")

        # 每条 claim 的 level 得合法
        for i, c in enumerate(claims):
            if isinstance(c, dict):
                lv = c.get(CLAIM_LEVEL_KEY)
                if lv is not None and (not isinstance(lv, str)
                                       or lv.strip().upper() not in LEVELS):
                    problems.append(
                        f"claims[{i}] 的 level 非法：{lv!r}")
            if not _claim_text(c):
                problems.append(f"claims[{i}] 内容为空")

    # ---- not_run：必须存在（字段存在性上面已查），且不能有空话
    not_run = data.get("not_run")
    if not_run is None:
        # 字段缺失的情况上面已经报过了，这里不重复报。
        # 之所以要单独判 None：否则「缺字段」会落到下面的类型检查里，
        # 报成「必须是数组」——两种原因混在一起，测试就分辨不出
        # 到底是哪条检查在起作用（**反向验证会失效**）。
        #
        # 踩过的坑：类型检查会把「缺字段」也拦下，于是「必填检查」坏掉时
        # 测试照样全绿 —— 看起来有检查，实际测不到。
        pass
    elif not isinstance(not_run, list):
        problems.append("`not_run` 必须是数组（没有要写的就写 `[]`，"
                        "但字段不能少——少字段 = 假装全验了）")
    else:
        for i, item in enumerate(not_run):
            text = str(item or "").strip()
            if not text:
                problems.append(f"not_run[{i}] 为空")
                continue
            # 一条必须是**单行**。多行在 JSON 里要转义，很容易写坏文件
            # （本轮就踩过：一条 not_run 写成跨行两行，整个报告变成非法 JSON）。
            if "\n" in text or "\r" in text:
                problems.append(f"not_run[{i}] 含换行，必须写成单行：{text!r}")
                continue
            why = _is_vague(text)
            if why:
                problems.append(f"not_run[{i}] {why}")

    # ---- evidence：路径必须存在
    evidence = data.get("evidence")
    if not isinstance(evidence, list):
        problems.append("`evidence` 必须是数组")
    else:
        for i, item in enumerate(evidence):
            p = str(item or "").strip()
            if not p:
                problems.append(f"evidence[{i}] 为空")
                continue
            # 本机证据（.cache/ 等）不强制存在——它不入库
            if p.replace("\\", "/").startswith(LOCAL_EVIDENCE_PREFIXES):
                continue
            cand = pathlib.Path(p)
            if not cand.is_absolute() and base_dir is not None:
                cand = base_dir / cand
            if not cand.exists():
                problems.append(f"evidence[{i}] 不存在：{p}")

    return problems


def validate_file(path: pathlib.Path) -> list[str]:
    """校验一份报告文件。

    evidence 的相对路径基准是**仓库根**（不是报告文件所在目录）——
    报告里写的路径跟仓库结构一致，读的人不用猜基准在哪。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"报告文件不存在：{path}"]
    except (OSError, ValueError) as e:
        return [f"报告读不出来：{path}（{type(e).__name__}: {e}）"]
    return validate(data, base_dir=REPO_ROOT)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m core.verify_report",
        description="校验验证报告：6 级证据分级 + 必填「哪些没跑」")
    ap.add_argument("path", nargs="?", help="报告文件路径")
    ap.add_argument("--dir", dest="dir", help="校验目录下所有 *.json 报告")
    args = ap.parse_args(argv)

    if not args.path and not args.dir:
        ap.print_help()
        return 2

    files: list[pathlib.Path] = []
    if args.path:
        files.append(pathlib.Path(args.path))
    if args.dir:
        d = pathlib.Path(args.dir)
        if not d.is_dir():
            print(f"[x] 目录不存在：{d}")
            return 2
        files.extend(sorted(d.glob("*.json")))

    if not files:
        print(f"[!] {args.dir} 下没有 *.json 报告")
        return 0

    all_ok = True
    failed = 0
    for f in files:
        problems = validate_file(f)
        if problems:
            all_ok = False
            failed += 1
            print(f"[x] {f.name}")
            for p in problems:
                print(f"      - {p}")
        else:
            print(f"[✓] {f.name}")

    print()
    print(f"{'全部通过' if all_ok else '有报告不合格'}："
          f"{len(files)} 份，{failed} 份不合格")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
