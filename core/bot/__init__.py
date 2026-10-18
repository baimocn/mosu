# -*- coding: utf-8 -*-
"""墨溯 core.bot —— 数字分身（云端 LLM + RAG 风格锚点 + 规则硬校验）

包结构（单一职责，可独立测试/替换）：
    retriever.py  语料检索（风格样本来源，可换成 embedding 版）
    checker.py    规则硬校验（AI 腔/长度/感叹号/照抄）
    llm.py        云端 LLM 客户端（OpenAI 兼容，可换任意后端）
    service.py    编排（检索→组装→生成→校验→重试）
    server.py     HTTP 服务

第一性原理：让 LLM 的输出分布逼近 P(白墨的回复|输入)。三个组件分别贡献
风格先验（prompt）、样本锚点（RAG）、输出过滤（checker）——每个组件的
必要性都由消融实验验证（scripts/ablation_*.py），未通过验证的删除。
"""
from core.bot.checker import StyleChecker  # noqa: F401
from core.bot.llm import LlmClient  # noqa: F401
from core.bot.retriever import StyleRetriever  # noqa: F401
from core.bot.service import BotService  # noqa: F401
from core.bot.server import run_server  # noqa: F401
