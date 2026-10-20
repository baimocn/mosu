# -*- coding: utf-8 -*-
"""
墨溯 core —— AI 对话语料导入（把 .workbuddy / .claude 等目录的原始记录接进语料库）

## 为什么单独一个模块

微信语料（`core/persona.py`）和 AI 对话语料是两种东西：
  - 微信：你说给**人**听的，有社交面具，长度短（中位 11 字）
  - AI 对话：你说给**机器**听的，意图直白、上下文长、含你的真实需求与决策过程

后者对"理解一个人怎么做判断"价值更高，但它藏在各个 AI 工具的私有格式里，
没有统一格式。`core/persona.py` 只处理微信，所以这里补上。

## 支持的来源（都实测过真实文件）

| 来源 | 路径 | 格式 |
|------|------|------|
| WorkBuddy | `~/.workbuddy/projects/**/*.jsonl` | `type=message` + `role` + `content[]` |
| Claude Code | `~/.claude/projects/**/*.jsonl` | `type=user/assistant` + `message.content` |
| iFlow | `~/.iflow/projects/**/*.jsonl` | 同 Claude（同一套 sdk） |
| Reasonix | `~/.reasonix/sessions/*.jsonl` | `role` + `content`（字符串） |

## 三类必须精确处理的垃圾（实测占比很高，不处理语料就被污染）

1. **`<task-notification>`**：后台任务完成通知，系统自动产生，**不是用户说的话**。
   全库 817 条不含 `<user_query>` 的用户消息里绝大多数是它。
2. **`<system-reminder data-role="error-recovery">`**：断网重试提示，同上。
3. **`<conversation_history_summary>`**：上下文压缩摘要。**这条要保留**——
   它是用户意图的浓缩，且是 AI 自己写的、信息密度最高的部分；
   但它不是"用户的原话"，所以单独标记 source_type，不混进 user 语料。

## 提取策略：优先认标签，不靠"剥标签"

WorkBuddy 把用户真实输入包在 `<user_query>` 里（实测 1585/2402）。
优先取标签内内容；取不到才回退到"剥掉已知注入块后的剩余文本"，
并且剥完若仍以 `<` 开头（说明是没见过的注入格式）就丢弃——
宁可漏一条，不可把系统提示词当成你说的话。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 常量

#: 用户真实输入的标签（WorkBuddy 用法）
_RE_USER_QUERY = re.compile(r"<user_query>([\s\S]*?)</user_query>")

#: 上下文压缩摘要（保留，但标为 summary）
_RE_SUMMARY = re.compile(r"<conversation_history_summary>([\s\S]*?)</conversation_history_summary>")

#: 队友消息：子 agent 生成的报告，会以 user 轮的形式注入。
#: 这不是用户说的话，是另一个 AI 写的。混进语料会把"AI 的文风"当成"人的文风"。
_RE_TEAMMATE = re.compile(r'<teammate-message[^>]*>[\s\S]*?</teammate-message>')

#: 引用外壳：`<message_quote …><original_text><![CDATA[真的话]]></original_text></message_quote>`
#: 里面是用户自己说过的话，剥外壳留正文（分别处理开闭标签与 CDATA 包裹）
_RE_QUOTE_TAG = re.compile(r"</?message_quote[^>]*>|</?original_text>|<!\[CDATA\[|\]\]>")

#: 图片/文件占位符：`@image#1:xxx.jpg`、`@wecom#...`、`[image#1]`
#: 只吃占位符本身，不吃前后空格（用 \S 会顺带吞掉相邻空格，导致
#: "现在能识别吗 @image#1:x.png" 剥完只剩前半句、被误判为过短而丢弃）
_RE_ATTACH = re.compile(r"@(?:image|file|wecom|wedrive)#\d+:\S+|\[image#\d+\]", re.I)

#: 必须整条丢弃的注入块（系统产生，不是用户意图）
_DROP_BLOCKS = (
    re.compile(r"<task-notification>[\s\S]*?</task-notification>"),
    re.compile(r'<system-reminder[^>]*>[\s\S]*?</system-reminder>'),
    re.compile(r"<craft_mode>[\s\S]*?</craft_mode>"),
    re.compile(r"<user-context>[\s\S]*?</user-context>"),
    re.compile(r"<additional-data>[\s\S]*?</additional-data>"),
    re.compile(r"<wecom_wedrive_references>[\s\S]*?</wecom_wedrive_references>"),
)

#: 实测的注入块整条丢弃特征（开头匹配即丢）。
#: 注意：`<conversation_history_summary>` **不在**这里——摘要要保留（标为 summary），
#: 放进来的话会在到达摘要分支之前就被丢掉。它由 `_RE_SUMMARY` 单独处理。
_DROP_PREFIX = (
    "<task-notification",
    "<system-reminder",
    "<craft_mode",
    "<user-context",
    "<message_quote",
    "<cb_summary",       # 压缩外壳：里面是压过的历史，不是本轮真话
    "Please continue with the conversation based on the summarized context",
)

#: 系统在同一行里追加的固定说明句——剥掉标签后它们会残留下来。
#: 例如 task-notification 被剥掉后，末尾还挂着
#: "Use the TaskOutput tool with task_id=... if you need to act on it"，
#: 那段是系统写给 AI 的指令，不是用户说的话。
_RESIDUE_PATTERNS = (
    re.compile(r"Use the TaskOutput tool with task_id=[^\n]*", re.I),
    re.compile(r"For background shell tasks, pass full=true[^\n]*", re.I),
    re.compile(r"\[SYSTEM NOTIFICATION[^\]]*\][^\n]*", re.I),
    re.compile(r"Do not treat this as user acknowledgement[^\n]*", re.I),
    re.compile(r"This is an automated background-task event[^\n]*", re.I),
    re.compile(r"including any earlier claim in your own messages[^\n]*", re.I),
)

#: 定时任务（automation）注入的执行指令。整条都是系统写的，
#: 里面虽然谈"用户需求"，但原文是调度器生成的模板，不是人说的话。
_RE_AUTOMATION = re.compile(
    r"You MUST follow these steps in order:[\s\S]*", re.I)

#: 上下文压缩后的"续接指令"——系统叫 AI 继续，不是用户在说话。
#: 实测这条会以独立一条 user 消息出现（无任何标签包裹），共 3 条。
_RE_CONTINUE = re.compile(
    r"Please continue with the conversation based on the summarized context[\s\S]*", re.I)

#: `<cb_summary>` 压缩摘要块：内容是被压过的历史，不是本轮的真话。
#: 它内部常再嵌一层 `<conversation_history_summary>`，两者都要剥。
_RE_CB_SUMMARY = re.compile(r"<cb_summary>[\s\S]*?</cb_summary>", re.I)

#: content 数组里算正文的 part 类型（WorkBuddy 用 input_text/output_text，
#: 其它工具用 text；不包括 thinking / image_blob_ref / tool_use）
_TEXT_PART_TYPES = ("text", "input_text", "output_text")

#: 太短的消息大概率是"嗯""好的""继续"，对画像没有信息量。
#: **只用于「回退提取」**——即剥掉注入块后剩下的残渣，短的基本是噪音。
MIN_LEN = 8

#: 明确落在 `<user_query>` 里的内容，是用户真的敲字发出去的，
#: 哪怕只有"继续""A""B"也照收。实测这类短输入有 300+ 条，
#: 而且它们本身就是重要信号（说明他常下简短指令，不啰嗦）。
#: 用 MIN_LEN=8 过滤会误杀，把"人的真实说话长度"统计整体拉长。
MIN_LEN_TRUSTED = 1

#: 单条上限：AI 对话里常有整段贴代码，截断避免语料被单条淹没
MAX_LEN = 4000


# ---------------------------------------------------------------- 数据结构
@dataclass
class ChatTurn:
    """一条用户输入（或一条摘要）。"""
    ts: int                      # 毫秒时间戳，0 表示未知
    source: str                  # workbuddy / claude / iflow / reasonix
    project: str                 # 项目目录名（还原后）
    session: str                 # 会话 id
    role: str                    # user / summary
    text: str
    meta: dict = field(default_factory=dict)

    def as_corpus(self) -> dict:
        """对齐 core/persona.py 的 corpus 单条结构，方便下游共用。"""
        return {
            "ts": self.ts,
            "time": (datetime.fromtimestamp(self.ts / 1000).strftime("%Y-%m-%d %H:%M")
                     if self.ts else ""),
            "session": f"{self.source}/{self.project}",
            "sessionType": "ai",
            "kind": self.role,
            "text": self.text,
            "emojis": [],
            "imgMd5": None,
            "len": len(self.text),
            "source": self.source,
            "project": self.project,
            "sessionId": self.session,
        }


# ---------------------------------------------------------------- 清洗
def strip_injections(text: str) -> tuple[str, bool]:
    """剥掉已知的系统注入块，返回 (剩余文本, 是否含压缩摘要)。

    注意：摘要块要被单独抽出来，所以先判断再剥离。
    """
    has_summary = bool(_RE_SUMMARY.search(text))
    out = text
    for pat in _DROP_BLOCKS:
        out = pat.sub("", out)
    return out.strip(), has_summary


def _clean_residue(text: str) -> str:
    """剥掉系统追加的固定说明句，以及包在用户原话外面的引用外壳。"""
    for pat in _RESIDUE_PATTERNS:
        text = pat.sub("", text)
    # 定时任务的执行指令：整段都是调度器生成的模板，不是人说的话
    text = _RE_AUTOMATION.sub("", text)
    # 压缩摘要块与"请继续"指令：都是系统话术
    text = _RE_CB_SUMMARY.sub("", text)
    text = _RE_CONTINUE.sub("", text)
    # `<message_quote>` 里是**用户自己之前说过的话**（实测：引用"只在正式交付成果时贴"），
    # 内容有价值，只剥外壳、留正文。
    text = _RE_QUOTE_TAG.sub("", text)
    # 图片/附件占位符（`@image#1:xxx.jpg`）对语料无意义，且会污染词频
    text = _RE_ATTACH.sub("", text)
    return text.strip()


def extract_user_text(text: str) -> tuple[str, str]:
    """从一条 user 消息里提取真实输入。

    返回 (文本, 类型)，类型为 user 或 summary 或空串（表示整条都是噪音）。

    优先级（顺序很关键，都是踩坑换来的）：
      1. `<user_query>` 标签内 —— 最可信的"用户原话"。**必须最先判**，
         因为 `<system-reminder data-role="user-context">` 开头的消息里
         同时包着环境信息和真实输入（实测 2182 条），先丢整条会全丢光。
      2. 没有真实输入标签、且整条是已知注入 → 丢
         （`<task-notification>` 剥掉标签后末尾还挂着系统写给 AI 的
         "Use the TaskOutput tool with task_id=…"，那段不是人说的话，
         实测污染了几百条，由 `_clean_residue` 再兜一层）
      3. `<conversation_history_summary>` —— 保留但标为 summary
      4. 剥掉注入块后的剩余 —— 仍以 `<` 开头则判为未知注入，丢弃
    """
    if not text or not text.strip():
        return "", ""

    # 1) 先取明确标签——这一步必须早于「整条丢弃」判断。
    #    踩过的坑：`<system-reminder data-role="user-context">` 开头的消息里
    #    同时包着环境信息和**真实的 `<user_query>`**（实测 2182 条）。
    #    如果在开头就判整条丢弃，会把用户真正说的话一起扔掉——
    #    我第一版就是这么写的，总量从 2002 条掉到 177 条。
    hits = _RE_USER_QUERY.findall(text)
    if hits:
        joined = "\n\n".join(h.strip() for h in hits if h.strip())
        # 标签内部还嵌着别的注入块（实测：用户说"继续"时，
        # 同一轮里会挂着后台任务通知），必须再剥一层
        joined, _ = strip_injections(joined)
        joined = _clean_residue(joined)
        joined = _RE_TEAMMATE.sub("", joined).strip()
        # 只看"非空"，不卡长度：这是用户亲手敲的字，"继续""A""B"也是真实输入
        if len(joined) < MIN_LEN_TRUSTED or joined == ".":
            return "", ""
        return joined[:MAX_LEN], "user"

    head = text.lstrip()

    # 2) 没有真实输入标签，且整条是已知注入 → 丢
    if any(head.startswith(p) for p in _DROP_PREFIX):
        return "", ""

    # 3) 压缩摘要：不是原话，但信息密度最高
    sums = _RE_SUMMARY.findall(text)
    if sums:
        joined = "\n\n".join(s.strip() for s in sums if s.strip())
        return joined.strip()[:MAX_LEN], "summary"

    # 4) 回退：剥注入后看剩下什么
    rest, _ = strip_injections(text)
    rest = _clean_residue(rest)
    rest = _RE_TEAMMATE.sub("", rest).strip()
    if not rest:
        return "", ""
    # 剥完还是标签开头 = 没见过的注入格式，不敢当用户输入
    if rest.startswith("<"):
        return "", ""
    if rest.startswith("Please continue with the conversation"):
        return "", ""
    if len(rest) < MIN_LEN:
        return "", ""
    return rest[:MAX_LEN], "user"


def project_from_dir(dirname: str) -> str:
    """把 `d-Desktop-微信解密软件` 这类目录名还原成可读项目名。

    WorkBuddy / Claude 都把路径里的 `:` `\\` `/` 换成 `-`，是有损编码
    （`D:\\Desktop\\x` 和 `D:\\Desktop/x` 会变成同一个串），无法精确还原。
    这里只做能确定的事：
      - 首段是盘符（`d` / `c`）→ 丢掉
      - 剩下的部分整体保留，够当项目标签用

    不做 `--` → `/` 的猜测：连字符本身也可能是项目名的一部分
    （实测有 `d-Desktop-墨衍-项目`），替换反而会造出假路径。
    """
    parts = dirname.split("-")
    if parts and len(parts[0]) <= 2:          # 盘符段（d / c / e）
        parts = parts[1:]
    if not parts:
        return dirname
    return "-".join(parts)


# ---------------------------------------------------------------- 各来源解析
def _text_of_content(content) -> str:
    """从 message.content 取出正文（兼容 str 与 part 数组）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in _TEXT_PART_TYPES:
                out.append(p.get("text") or "")
        return "".join(out)
    return ""


def _ms(ts) -> int:
    """时间戳归一化到毫秒。有些源给秒，有些给毫秒，有些给 ISO 字符串。"""
    if isinstance(ts, (int, float)):
        v = int(ts)
        # 10 位是秒，13 位是毫秒
        return v * 1000 if v < 10_000_000_000 else v
    if isinstance(ts, str) and ts:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
            try:
                return int(datetime.strptime(ts[:26], fmt).timestamp() * 1000)
            except Exception:
                continue
    return 0


def parse_workbuddy(path: Path, project: str) -> list[ChatTurn]:
    """WorkBuddy：`type=message` + `role` + `content[]`（part 类型 input_text/output_text）。"""
    turns: list[ChatTurn] = []
    sid = path.stem
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if '"user"' not in line and '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "message":
                continue
            if d.get("role") != "user":      # AI 的话不是"我的语料"
                continue
            raw = _text_of_content(d.get("content"))
            text, kind = extract_user_text(raw)
            if not text or len(text) < MIN_LEN:
                continue
            turns.append(ChatTurn(
                ts=_ms(d.get("timestamp")), source="workbuddy", project=project,
                session=d.get("sessionId") or sid, role=kind, text=text,
                meta={"cwd": d.get("cwd") or ""},
            ))
    return turns


def parse_claude_like(path: Path, project: str, source: str) -> list[ChatTurn]:
    """Claude Code / iFlow：`type=user|assistant` + `message.content`。

    这两个是同一套 sdk，格式一致（实测字段：uuid/parentUuid/sessionId/timestamp/
    type/message/cwd）。assistant 的 content 里混有 tool_use，只取 text part。
    """
    turns: list[ChatTurn] = []
    sid = path.stem
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "user":
                continue
            msg = d.get("message") or {}
            raw = _text_of_content(msg.get("content"))
            text, kind = extract_user_text(raw)
            if not text or len(text) < MIN_LEN:
                continue
            turns.append(ChatTurn(
                ts=_ms(d.get("timestamp")), source=source, project=project,
                session=d.get("sessionId") or sid, role=kind, text=text,
                meta={"cwd": d.get("cwd") or ""},
            ))
    return turns


def parse_reasonix(path: Path, project: str) -> list[ChatTurn]:
    """Reasonix：扁平 `role` + `content`（字符串），没有外层 type。"""
    turns: list[ChatTurn] = []
    sid = path.stem
    mtime = int(path.stat().st_mtime * 1000) if path.exists() else 0
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("role") != "user":
                continue
            raw = d.get("content")
            if not isinstance(raw, str):
                raw = _text_of_content(raw)
            text, kind = extract_user_text(raw)
            if not text or len(text) < MIN_LEN:
                continue
            turns.append(ChatTurn(
                ts=_ms(d.get("timestamp")) or mtime, source="reasonix", project=project,
                session=sid, role=kind, text=text,
            ))
    return turns


# ---------------------------------------------------------------- 来源发现
@dataclass
class Source:
    name: str
    root: Path
    pattern: str
    parser: str          # workbuddy / claude / iflow / reasonix
    note: str = ""


def discover(extra_roots: dict[str, str] | None = None) -> list[Source]:
    """探测本机存在哪些 AI 工具的对话记录目录。

    只返回**真实存在且有 jsonl** 的，不返回空目录——
    否则统计里会出现一堆 0，让人以为漏了数据。
    """
    home = Path.home()
    cands = [
        Source("workbuddy", home / ".workbuddy" / "projects", "**/*.jsonl", "workbuddy",
               "当前工具，语料最多"),
        Source("claude", home / ".claude" / "projects", "**/*.jsonl", "claude",
               "Claude Code"),
        Source("iflow", home / ".iflow" / "projects", "**/*.jsonl", "iflow", "iFlow"),
        Source("reasonix", home / ".reasonix" / "sessions", "*.jsonl", "reasonix", "Reasonix"),
    ]
    if extra_roots:
        for name, root in extra_roots.items():
            cands.append(Source(name, Path(root), "**/*.jsonl", "claude", "自定义"))
    out = []
    for s in cands:
        if not s.root.exists():
            continue
        if not any(s.root.glob(s.pattern)):
            continue
        out.append(s)
    return out


def load_source(src: Source) -> list[ChatTurn]:
    """读一个来源下所有文件。"""
    turns: list[ChatTurn] = []
    for path in sorted(src.root.glob(src.pattern)):
        if not path.is_file():
            continue
        # 项目名取相对 root 的第一层目录；reasonix 是平铺文件，用父目录名
        rel = path.relative_to(src.root)
        project = project_from_dir(rel.parts[0]) if len(rel.parts) > 1 else project_from_dir(src.name)
        try:
            if src.parser == "workbuddy":
                turns.extend(parse_workbuddy(path, project))
            elif src.parser == "reasonix":
                turns.extend(parse_reasonix(path, project))
            else:
                turns.extend(parse_claude_like(path, project, src.name))
        except Exception:      # noqa: BLE001  单文件坏了不该拖垮整批
            continue
    return turns


def collect(sources: list[Source] | None = None,
            max_chars: int | None = None) -> tuple[list[ChatTurn], dict]:
    """汇总所有来源，返回 (语料, 统计)。

    max_chars 用于截断总量（例如只取最近 N 万字符做画像）。
    """
    srcs = sources if sources is not None else discover()
    all_turns: list[ChatTurn] = []
    per_source: dict[str, int] = {}
    per_project: dict[str, int] = {}
    for s in srcs:
        got = load_source(s)
        per_source[s.name] = len(got)
        for t in got:
            per_project[f"{t.source}/{t.project}"] = per_project.get(f"{t.source}/{t.project}", 0) + 1
        all_turns.extend(got)

    all_turns.sort(key=lambda t: t.ts or 0)
    total_chars = sum(len(t.text) for t in all_turns)

    # 按字符预算从最新往回取（画像更关心最近的状态）
    if max_chars and total_chars > max_chars:
        kept: list[ChatTurn] = []
        acc = 0
        for t in reversed(all_turns):
            if acc + len(t.text) > max_chars:
                break
            kept.append(t)
            acc += len(t.text)
        all_turns = list(reversed(kept))
        total_chars = acc

    stats = {
        "turns": len(all_turns),
        "chars": total_chars,
        "sources": per_source,
        "projects": len(per_project),
        "topProjects": sorted(per_project.items(), key=lambda kv: -kv[1])[:12],
        "byRole": {
            "user": sum(1 for t in all_turns if t.role == "user"),
            "summary": sum(1 for t in all_turns if t.role == "summary"),
        },
        "firstTs": all_turns[0].ts if all_turns else 0,
        "lastTs": all_turns[-1].ts if all_turns else 0,
    }
    return all_turns, stats


def write_corpus(turns: list[ChatTurn], out_dir: Path) -> dict:
    """写盘：jsonl 全量 + 按项目分段的纯文本 + 按月分段。

    与 core/persona.py 的产出结构保持一致（jsonl + 分段 txt），
    这样下游的风格画像/心理分析可以同时吃微信语料和 AI 语料。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / "ai_corpus.jsonl"
    with open(jsonl, "w", encoding="utf-8") as f:
        for t in turns:
            f.write(json.dumps(t.as_corpus(), ensure_ascii=False) + "\n")

    # 按项目
    by_proj: dict[str, list[ChatTurn]] = {}
    for t in turns:
        by_proj.setdefault(f"{t.source}-{t.project}", []).append(t)
    proj_dir = out_dir / "by_project"
    proj_dir.mkdir(parents=True, exist_ok=True)
    bad = '<>:"/\\|?*'
    for name, items in by_proj.items():
        safe = "".join(c for c in name if c not in bad).strip()[:60] or "misc"
        lines = [f"# {name}（用户输入 {len(items)} 条）", ""]
        for t in items:
            stamp = datetime.fromtimestamp(t.ts / 1000).strftime("%Y-%m-%d %H:%M") if t.ts else "未知时间"
            tag = "摘要" if t.role == "summary" else "输入"
            lines.append(f"[{stamp}][{tag}] {t.text}")
        (proj_dir / f"{safe}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {"jsonl": str(jsonl), "projects": len(by_proj), "turns": len(turns)}


# ---------------------------------------------------------------- 画像特征
def style_features(turns: list[ChatTurn]) -> dict:
    """从 AI 对话语料里算"我怎么向 AI 提需求"的特征。

    和微信画像的区别：微信看的是"我怎么跟人相处"，
    这里看的是"我怎么思考、怎么表达需求、怎么验收结果"。
    """
    from collections import Counter

    texts = [t.text for t in turns if t.role == "user"]
    if not texts:
        return {"turns": 0}

    lens = [len(t) for t in texts]
    lens_sorted = sorted(lens)
    n = len(lens)

    def pct(p: float) -> int:
        return lens_sorted[min(n - 1, int(n * p))]

    # 需求类型：从措辞判断（不是猜，是可验证的词组）
    intent = {
        "让我做": sum(1 for t in texts if re.search(r"(帮我|帮忙|给我|麻烦你|去|做一下|搞一下)", t)),
        "问能不能": sum(1 for t in texts if re.search(r"(能不能|可不可以|能否|会不会|可以吗)", t)),
        "改错/返工": sum(1 for t in texts if re.search(r"(报错|出错|不行|失败|修一下|还是不对|有问题|没生效)", t)),
        "催进度": sum(1 for t in texts if re.search(r"(继续|还没|现在呢|怎么样了|好了没)", t)),
        "追原因": sum(1 for t in texts if re.search(r"(为什么|为啥|怎么会|原因是|到底)", t)),
        "授权/放权": sum(1 for t in texts if re.search(r"(你自己|随你|你决定|全权|看着办)", t)),
    }

    # 直接性：模糊表达越少越直接
    vague = sum(1 for t in texts if re.search(r"(大概|差不多|可能是|好像|也许|随便)", t))

    return {
        "turns": len(texts),
        "chars": sum(lens),
        "medianLen": pct(0.5),
        "p90Len": pct(0.9),
        "maxLen": lens_sorted[-1],
        "intent": intent,
        "vagueRate": round(vague / len(texts), 4),
        "topProjects": Counter(t.project for t in turns).most_common(8),
        "hourHist": _hour_hist(turns),
    }


def _hour_hist(turns: list[ChatTurn]) -> list[int]:
    hist = [0] * 24
    for t in turns:
        if not t.ts:
            continue
        try:
            hist[datetime.fromtimestamp(t.ts / 1000).hour] += 1
        except Exception:
            continue
    return hist


def render_markdown(turns: list[ChatTurn], stats: dict, feat: dict) -> str:
    """人读版报告。CLI 直接打印这个。"""
    def fmt_ts(ms: int) -> str:
        if not ms:
            return "未知"
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")

    L = ["# AI 对话语料报告", ""]
    L.append(f"- 来源：{'、'.join(f'{k}({v})' for k, v in stats['sources'].items())}")
    L.append(f"- 有效用户输入：**{stats['byRole']['user']}** 条"
             f"（另有压缩摘要 {stats['byRole']['summary']} 条）")
    L.append(f"- 总字符数：**{stats['chars']:,}**")
    if stats["firstTs"]:
        L.append(f"- 时间跨度：{fmt_ts(stats['firstTs'])} → {fmt_ts(stats['lastTs'])}")
    L.append("")

    if feat.get("turns"):
        L += ["## 我怎么向 AI 提需求", ""]
        L.append(f"- 单条中位长度 **{feat['medianLen']} 字**，P90 为 {feat['p90Len']} 字")
        L.append(f"- 模糊表达占比 **{feat['vagueRate']*100:.1f}%**"
                 f"（越低越倾向于说清具体要求）")
        L.append("")
        L.append("| 需求类型 | 条数 | 占比 |")
        L.append("|---|---|---|")
        tot = feat["turns"]
        for k, v in sorted(feat["intent"].items(), key=lambda kv: -kv[1]):
            L.append(f"| {k} | {v} | {v/tot*100:.1f}% |")
        L.append("")

        hist = feat["hourHist"]
        if sum(hist):
            peak = max(range(24), key=lambda h: hist[h])
            L.append(f"- 最活跃时段：**{peak}:00**（{hist[peak]} 条）")
            night = sum(hist[0:6])
            L.append(f"- 凌晨 0-6 点占比：{night/sum(hist)*100:.1f}%")
            L.append("")

    if stats.get("topProjects"):
        L += ["## 主要项目", ""]
        L.append("| 项目 | 输入条数 |")
        L.append("|---|---|")
        for name, c in stats["topProjects"]:
            L.append(f"| {name} | {c} |")
        L.append("")

    L += ["---", "*由墨溯 mosu aicorpus 生成｜纯本地读取，无网络*"]
    return "\n".join(L)
