# -*- coding: utf-8 -*-
"""规则硬校验 —— 把 prompt 的软约束变成可执行的质检。

设计原则（第一性原理）：
  prompt 是软约束，模型会漂移；校验器是硬约束，不靠模型自觉。
  但校验器只能"拦"不能"造"——它防的是已知的失败模式
  （AI 腔、超长、照抄），对"平庸的正确回复"无效，
  因此校验通过 ≠ 像白墨，只是必要条件。
"""
from __future__ import annotations

AI_CLICHES = [
    "很高兴为您", "为您服务", "作为AI", "作为一个人工智能", "希望这对你有帮助",
    "希望这能帮到", "综上所述", "总而言之", "值得注意", "首先，", "其次，",
    "最后，", "让我们", "此外，", "需要注意的是", "我将", "我会尽力",
    "理解您", "收到您的", "祝您", "希望对您", "如果您有任何", "欢迎随时",
]


class StyleChecker:
    def __init__(self, max_len: int = 200, max_exclaim: int = 2,
                 copy_threshold: float = 0.85):
        self.max_len = max_len
        self.max_exclaim = max_exclaim
        self.copy_threshold = copy_threshold

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """字符级 Dice 系数（短文本够用，不引第三方库）。"""
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
