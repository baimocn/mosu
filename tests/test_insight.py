# -*- coding: utf-8 -*-
"""洞察问答模块的离线测试（不碰网络、不调 LLM）。

覆盖的是「模型不可控的那部分之外」的逻辑：
  - 事实采集不崩、字段口径对得上
  - 提示词把事实和样本都带上，且明确区分两者
  - 依据抽取能把「（依据：…）」抓出来
  - 空问题必须报错，不能悄悄返回空答案
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import insight  # noqa: E402


class TestPromptBuilding:
    def test_facts_are_included(self):
        msgs = insight.build_prompt("我什么时候最话多？", ["事实A：11 条"], [])
        user = msgs[-1]["content"]
        assert "我什么时候最话多？" in user
        assert "事实A：11 条" in user
        assert msgs[0]["role"] == "system"

    def test_samples_marked_as_style_only(self):
        """样本必须被标注成「风格参考」——不然模型会拿它当答案素材编造。"""
        msgs = insight.build_prompt("q", ["f"], [
            {"text": "今晚吃饭吗", "speaker": "小明", "time": "2026-01-01"},
        ])
        user = msgs[-1]["content"]
        assert "今晚吃饭吗" in user
        assert "小明" in user
        assert "风格" in user
        assert "不是答案素材" in user

    def test_empty_samples_marked(self):
        msgs = insight.build_prompt("q", [], [])
        assert "无样本" in msgs[-1]["content"]

    def test_system_prompt_forbids_inventing_numbers(self):
        """系统提示必须明确禁止自己算数——这是本模块的核心防线。"""
        msgs = insight.build_prompt("q", [], [])
        s = msgs[0]["content"]
        assert "不允许自己另算" in s or "绝不允许自己另算" in s
        assert "依据" in s


class TestCitations:
    def test_extracts_citations(self):
        ans = "结论是这样（依据：全库共 243,570 条消息）。第二点（依据：峰值时段是 17 点）。"
        out = insight.split_citations(ans)
        assert "全库共 243,570 条消息" in out["citations"]
        assert "峰值时段是 17 点" in out["citations"]

    def test_dedupes(self):
        ans = "（依据：事实X）又来一次（依据：事实X）"
        out = insight.split_citations(ans)
        assert out["citations"].count("事实X") == 1

    def test_no_citations_is_empty_list(self):
        assert insight.split_citations("普通的一段话，没有依据标注。")["citations"] == []

    def test_handles_none(self):
        assert insight.split_citations(None)["citations"] == []


class TestPresets:
    def test_presets_wellformed(self):
        assert len(insight.PRESETS) >= 3
        for p in insight.PRESETS:
            assert p.get("id")
            assert p.get("q")
            assert p.get("hint")

    def test_preset_ids_unique(self):
        ids = [p["id"] for p in insight.PRESETS]
        assert len(ids) == len(set(ids))


class TestAskValidation:
    def test_empty_question_raises(self):
        """空问题必须抛错，不能返回一个空答案让前端以为成功。"""
        ds = object()   # 不会用到，因为校验在前
        with pytest.raises(ValueError):
            insight.ask(ds, "   ")

    def test_none_question_raises(self):
        with pytest.raises(ValueError):
            insight.ask(object(), None)


class TestSampleRetrieval:
    def test_missing_corpus_returns_empty(self):
        out = insight.retrieve_samples("/nonexistent/corpus.jsonl", "q")
        assert out == []

    def test_normalizes_fields(self):
        """语料条目字段可能叫 speaker 或 sender —— 都要能取到。"""
        p = Path(ROOT) / ".cache" / "_test_corpus.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            '{"kind":"text","text":"晚上有空吗","len":6,"speaker":"小红"}\n'
            '{"kind":"text","text":"明天再说","len":5,"sender":"小刚"}\n'
            '{"kind":"image","text":"[图片]","len":4}\n',
            encoding="utf-8")
        try:
            out = insight.retrieve_samples(p, "晚上 有空", topk=5)
        finally:
            try:
                p.unlink()
            except OSError:
                pass
        assert out
        for s in out:
            assert set(s.keys()) == {"text", "speaker", "time"}
            assert isinstance(s["speaker"], str)
