# -*- coding: utf-8 -*-
"""
墨溯 core —— 个人 bot 服务（云端 LLM + RAG 风格锚点 + 规则硬约束）

架构（三者解耦，后端模型随时可换）：

    用户消息
      → StyleRetriever   从本人语料检索最相似的几条历史发言（同义词扩展 +
                         场景加权）——只作"风格参考"，不当答案
      → BotService       组装 prompt：风格画像（system）+ 参考样本（user 消息，
                         明确要求"体会语气、不要照抄、按当前语境组织"）+ 对话历史
      → 云端 LLM         OpenAI 兼容 API（env 配置，任意后端）
      → StyleChecker     硬校验：AI 腔禁词 / 长度 / 感叹号 / 照抄检测
      → 不达标            带具体反馈重生成（最多 max_retries 次）

隐私说明：语料库只在本机；发给云端的只有「当前对话 + 检索到的几条参考样本」。
配置全走环境变量：
    MOSU_BOT_BASE_URL   OpenAI 兼容端点（默认 https://api.openai.com/v1）
    MOSU_BOT_API_KEY    API key（必需）
    MOSU_BOT_MODEL      模型名（默认 gpt-4o-mini）
    MOSU_BOT_PORT       服务端口（默认 8787）
    MOSU_BOT_CORPUS     语料 jsonl 路径（默认 <persona 目录>/corpus.jsonl）
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------- 同义词扩展
SYNONYMS: dict[str, list[str]] = {
    "报价": ["多少钱", "价格", "费用", "收费", "预算"],
    "吃": ["饭", "外卖", "奶茶", "食堂", "餐"],
    "bug": ["报错", "问题", "修", "故障", "错误"],
    "学习": ["课", "作业", "考试", "复习", "自学"],
    "教程": ["视频", "课程", "攻略", "文档", "手册"],
    "多少钱": ["报价", "价格", "费用", "预算"],
    "图书馆": ["自习", "看书", "复习"],
    "部署": ["上线", "发布", "服务器", "搭建"],
    "模型": ["AI", "大模型", "接口"],
    "网站": ["页面", "前端", "建站"],
    "客户": ["甲方", "单子", "需求方"],
    "晚上": ["今晚", "夜里"],
    "明天": ["明早", "次日"],
    "游戏": ["开黑", "上号", "排位"],
    "跑步": ["锻炼", "健身", "夜跑"],
    "睡觉": ["睡了", "晚安", "困"],
    "快递": ["取件", "包裹", "驿站"],
    "群": ["群里", "群友"],
}

# 场景路由：query 命中关键词 → 优先检索对应会话
SCENE_HINTS: dict[str, list[str]] = {
    "tech": ["bug", "部署", "代码", "模型", "API", "网站", "服务器", "python", "脚本",
             "上线", "AI", "MCP", "数据库"],
    "food": ["吃", "饭", "外卖", "奶茶", "食堂"],
    "study": ["课", "作业", "考试", "复习", "图书馆", "老师"],
    "life": ["快递", "睡觉", "跑步", "晚上", "明天"],
}

# AI 腔禁词：命中任意一个，这条回复不合格
AI_CLICHES = [
    "很高兴为您", "为您服务", "作为AI", "作为一个人工智能", "希望这对你有帮助",
    "希望这能帮到", "综上所述", "总而言之", "值得注意", "首先，", "其次，",
    "最后，", "让我们", "此外，", "需要注意的是", "我将", "我会尽力",
    "理解您", "收到您的", "祝您", "希望对您", "如果您有任何", "欢迎随时",
]

STOP = set("的了是在我你他她这那就都也和有不吧啊哦呢吗什么怎么一个可以现在时候知道因为所以但是然后如果还是这个那个觉得感觉有点一下今天明天昨天")


# ---------------------------------------------------------------- 检索器
class StyleRetriever:
    """语料检索：jieba 分词倒排 + 同义词扩展 + 场景加权。样本只作风格锚点。"""

    def __init__(self, corpus_path: str | Path, max_len: int = 200):
        import logging
        import warnings
        logging.getLogger("jieba").setLevel(logging.ERROR)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import jieba
        self._jieba = jieba
        self.corpus: list[dict] = []
        with open(corpus_path, encoding="utf-8") as f:
            for line in f:
                try:
                    c = json.loads(line)
                except Exception:
                    continue
                if c.get("kind") == "text" and 2 <= (c.get("len") or 0) <= max_len:
                    self.corpus.append(c)
        self.inv: dict[str, list[int]] = {}
        for i, c in enumerate(self.corpus):
            for w in set(self._tok(c["text"])):
                self.inv.setdefault(w, []).append(i)

    def _tok(self, text: str) -> list[str]:
        return [w for w in self._jieba.cut(text)
                if len(w) >= 2 and w not in STOP and not w.isascii()]

    def _expand(self, query: str) -> set[str]:
        words = set(self._tok(query))
        extra = set()
        for w in list(words):
            for key, syns in SYNONYMS.items():
                if w == key or key in query:
                    extra.update(syns)
                    extra.add(key)
        return words | {w for w in extra if len(w) >= 2}

    def _scene_of(self, query: str) -> str | None:
        for scene, kws in SCENE_HINTS.items():
            if any(k in query for k in kws):
                return scene
        return None

    def search(self, query: str, topk: int = 5) -> list[dict]:
        qw = self._expand(query)
        if not qw:
            return []
        scene = self._scene_of(query)
        scores: Counter = Counter()
        for w in qw:
            for i in self.inv.get(w, []):
                scores[i] += 1
        qn = len(qw) ** 0.5
        out = []
        for i, _ in scores.most_common(topk * 6):
            c = self.corpus[i]
            cw = set(self._tok(c["text"]))
            if not cw:
                continue
            overlap = len(qw & cw)
            if overlap == 0:
                continue
            sim = overlap / (qn * len(cw) ** 0.5)
            if scene == "tech" and any(k in c["text"] for k in ("AI", "模型", "部署", "代码", "服务器")):
                sim *= 1.3
            elif scene == "food" and any(k in c["text"] for k in ("吃", "饭", "外卖", "奶茶")):
                sim *= 1.3
            elif scene == "study" and any(k in c["text"] for k in ("课", "作业", "考试", "图书馆")):
                sim *= 1.3
            out.append((sim, c))
        out.sort(key=lambda x: -x[0])
        return [c for _, c in out[:topk]]


# ---------------------------------------------------------------- 规则校验
class StyleChecker:
    """把 prompt 的软约束变成硬约束：不达标就带反馈打回。"""

    def __init__(self, max_len: int = 80, max_exclaim: int = 2,
                 copy_threshold: float = 0.85):
        self.max_len = max_len
        self.max_exclaim = max_exclaim
        self.copy_threshold = copy_threshold

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """字符级相似度（Dice 系数，短文本够用）。"""
        if not a or not b:
            return 0.0
        bg_a = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
        bg_b = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
        return 2 * len(bg_a & bg_b) / (len(bg_a) + len(bg_b))

    def check(self, reply: str, retrieved: list[dict] | None = None) -> tuple[bool, list[str]]:
        """返回 (是否通过, 问题列表)。"""
        problems = []
        r = (reply or "").strip()
        if not r:
            return False, ["回复为空"]
        for w in AI_CLICHES:
            if w in r:
                problems.append(f"出现 AI 腔措辞「{w}」")
        if len(r) > self.max_len:
            problems.append(f"太长（{len(r)} 字 > {self.max_len}），他的风格是短句")
        if r.count("!") + r.count("！") > self.max_exclaim:
            problems.append("感叹号太多，他几乎不用感叹号")
        for c in (retrieved or []):
            src = c.get("text") or ""
            if len(src) >= 6 and self._similarity(r, src) > self.copy_threshold:
                problems.append(f"疑似照抄历史原句「{src[:30]}…」，要按当前语境重新组织")
                break
        return (not problems), problems

    @staticmethod
    def feedback(problems: list[str]) -> str:
        return ("上一条回复不合格：" + "；".join(problems[:3]) +
                "。用白墨的微信风格重说：短句、口语、可以带 [旺柴][捂脸] 这类表情，"
                 "不要照抄参考原文。")


# ---------------------------------------------------------------- LLM 客户端
class LlmClient:
    """OpenAI 兼容客户端（urllib 实现，零第三方依赖）。"""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None):
        self.base_url = (base_url or os.environ.get("MOSU_BOT_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("MOSU_BOT_API_KEY", "")
        self.model = model or os.environ.get("MOSU_BOT_MODEL", "gpt-4o-mini")
        if not self.api_key:
            raise SystemExit("[x] 未配置 MOSU_BOT_API_KEY（云端 LLM 的 key）")

    def chat(self, messages: list[dict], temperature: float = 0.8,
             max_tokens: int = 300) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------- Bot 编排
SYSTEM_TMPL = """你是白墨本人在微信聊天时的数字分身。规则（来自他 1.9 万条真实发言的统计）：
1. 长度：平均 12 字，71% 的消息在 10 字以内——默认短句，别长篇大论
2. 标点：65% 的消息不打标点；感叹号几乎不用；问号率仅 3%
3. 表情：37% 的消息带内嵌表情，常用 [旺柴][捂脸][便便][皱眉][苦涩]——自然地点缀，不要每条都加
4. 口头禅：应该、直接、不用、不然、主要、好像——自然地用，别堆砌
5. 应答习惯：常用「好」「ok」「对」「行」「嗯」回应
6. 群聊更放松更短，私聊稍长；不确定就直接说不知道，别编
7. 绝对不要出现「很高兴为您」「综上所述」「作为AI」这类助手腔"""

REF_TMPL = """下面是从白墨历史微信发言里检索到的 {n} 条片段（按与当前话题的相关度排序）。
它们只是**风格参考**——用来体会他的语气、用词、表情习惯。
**不要照抄这些原句**，要根据当前对话上下文组织一条新的回复。
如果参考片段与当前话题不完全匹配，忽略片段内容、只取语气。

参考片段：
{blocks}"""


class BotService:
    def __init__(self, corpus_path: str | Path, retriever: StyleRetriever | None = None,
                 checker: StyleChecker | None = None, max_retries: int = 2):
        self.retriever = retriever or StyleRetriever(corpus_path)
        self.checker = checker or StyleChecker()
        self.max_retries = max_retries
        self.history: dict[str, list[dict]] = {}      # conversation_id -> messages
        self.llm: LlmClient | None = None

    def _llm(self) -> LlmClient:
        if self.llm is None:
            self.llm = LlmClient()
        return self.llm

    def _build_messages(self, query: str, retrieved: list[dict],
                        conv_id: str | None) -> list[dict]:
        msgs = [{"role": "system", "content": SYSTEM_TMPL}]
        if retrieved:
            blocks = "\n".join(f"{i}. {c['text']}" for i, c in enumerate(retrieved, 1))
            msgs.append({"role": "user", "content": REF_TMPL.format(n=len(retrieved), blocks=blocks)})
            msgs.append({"role": "assistant", "content": "知道了，我按这个语气回复，不照抄。"})
        for h in self.history.get(conv_id or "", [])[-10:]:
            msgs.append(h)
        msgs.append({"role": "user", "content": query})
        return msgs

    def chat(self, query: str, conv_id: str | None = None,
             topk: int = 5) -> dict:
        t0 = time.time()
        retrieved = self.retriever.search(query, topk=topk)
        messages = self._build_messages(query, retrieved, conv_id)
        reply, problems, tries = "", [], 0
        for attempt in range(self.max_retries + 1):
            tries = attempt + 1
            reply = self._llm().chat(messages)
            ok, problems = self.checker.check(reply, retrieved)
            if ok:
                break
            # 带反馈重试：把不合格的回复和具体要求追加进上下文
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": self.checker.feedback(problems)})
        # 记录对话历史
        cid = conv_id or "default"
        self.history.setdefault(cid, []).extend([
            {"role": "user", "content": query},
            {"role": "assistant", "content": reply}])
        return {
            "reply": reply,
            "passed": not problems,
            "problems": problems,
            "tries": tries,
            "retrieved": [{"text": c["text"], "session": c.get("session", "")} for c in retrieved],
            "cost": round(time.time() - t0, 2),
        }

    def preview_prompt(self, query: str, topk: int = 5) -> dict:
        """不调 LLM，只看 prompt 会长什么样（调试用）。"""
        retrieved = self.retriever.search(query, topk=topk)
        return {"messages": self._build_messages(query, retrieved, None),
                "retrieved": retrieved}


# ---------------------------------------------------------------- HTTP 服务
def run_server(corpus_path: str | Path, port: int | None = None) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    port = port or int(os.environ.get("MOSU_BOT_PORT", "8787"))
    bot = BotService(corpus_path)
    print(f"[mosu-bot] 语料 {len(bot.retriever.corpus)} 条，监听 127.0.0.1:{port}")
    print(f"[mosu-bot] 后端 {os.environ.get('MOSU_BOT_BASE_URL', 'https://api.openai.com/v1')}"
          f" 模型 {os.environ.get('MOSU_BOT_MODEL', 'gpt-4o-mini')}")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静音默认日志
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._json({"ok": True, "corpus": len(bot.retriever.corpus)})
            else:
                self._json({"error": "GET /health only"}, 404)

        def do_POST(self):
            if self.path != "/chat":
                self._json({"error": "POST /chat only"}, 404)
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n).decode("utf-8"))
                query = (payload.get("message") or "").strip()
                if not query:
                    self._json({"error": "message 不能为空"}, 400)
                    return
                result = bot.chat(query, conv_id=payload.get("conversation_id"),
                                  topk=int(payload.get("topk", 5)))
                self._json(result)
            except SystemExit as e:
                self._json({"error": str(e)}, 500)
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    srv = HTTPServer(("127.0.0.1", port), Handler)
    srv.serve_forever()
