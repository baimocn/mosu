# -*- coding: utf-8 -*-
"""迭代 6：模型目录（家族识别 + 多端点 + 三态）的单元测试。

锁死三件最容易悄悄坏掉的事：

1. **模型名优先于 provider**
   Lancer 原注释：Transport providers (including OpenAI-compatible gateways)
   do not determine model identity when the actual model or its catalog label
   identifies a family.
   → 用户拿兼容网关转发 Claude，家族必须判成 claude。
   这条规则写反了不会报错，只会让所有模型显示成「GPT」，很难发现。

2. **旧配置继续能用**
   墨溯的用户已经有一堆只存了 baseUrl/apiKey/model 的 config.json。
   迭代 6 加了多端点，不能把这些人挡在门外。

3. **三态不能串**
   可用 / 未启用 / 未配置 三个状态各自对应不同的用户动作，
   串了会让人点不动该点的按钮、或以为配好了其实没配。
"""
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.model_catalog import (FAMILIES, STATE_AVAILABLE, STATE_DISABLED,
                                 STATE_UNCONFIGURED, build_catalog,
                                 family_id, is_model_enabled, model_family)


class TestFamilyDetection(unittest.TestCase):
    """家族识别规则。"""

    def test_nine_families_exist(self):
        """Lancer 是 9 个家族。少一个就意味着某家模型没有归属。"""
        self.assertEqual(len(FAMILIES), 9)
        ids = [f["id"] for f in FAMILIES]
        self.assertEqual(ids, ["openai", "claude", "grok", "gemini", "doubao",
                               "kimi", "deepseek", "qwen", "zhipu"])

    def test_families_have_unique_ids(self):
        ids = [f["id"] for f in FAMILIES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_model_name_wins_over_provider(self):
        """核心规则：兼容网关转发 Claude，家族必须是 claude。"""
        self.assertEqual(family_id("openai", "claude-sonnet-4", "Claude 4"),
                         "claude")
        self.assertEqual(family_id("openai-compatible", "claude-3-5-sonnet",
                                   "Claude 3.5 Sonnet"), "claude")
        self.assertEqual(family_id("my-gateway", "gemini-2.0-flash", ""),
                         "gemini")
        self.assertEqual(family_id("openai", "deepseek-r1", ""), "deepseek")

    def test_provider_used_only_when_model_unknown(self):
        """模型名判不出家族时，才看 provider。"""
        self.assertEqual(family_id("anthropic", "some-weird-id", ""), "claude")
        self.assertEqual(family_id("volcengine", "doubao-pro-32k", ""), "doubao")
        self.assertEqual(family_id("moonshot", "kimi-k2", ""), "kimi")

    def test_label_used_when_model_id_unrecognizable(self):
        """modelId 判不出但展示名能判出时，用展示名。"""
        self.assertEqual(family_id("", "x-custom-1", "Qwen 3 Max"), "qwen")

    def test_unknown_returns_empty(self):
        """判不出来返回空串，不能瞎猜一个家族。"""
        self.assertEqual(family_id("some-random-provider", "mystery-1", "?"), "")
        self.assertIsNone(model_family("", "", ""))

    def test_all_families_reachable_by_model_name(self):
        """9 个家族都得能被模型名认出来（不然某个家族永远匹配不到）。"""
        samples = {
            "openai": "gpt-4o", "claude": "claude-sonnet-4", "grok": "grok-2",
            "gemini": "gemini-2.0-pro", "doubao": "doubao-pro-32k",
            "kimi": "kimi-k2", "deepseek": "deepseek-chat",
            "qwen": "qwen-max", "zhipu": "glm-4-plus",
        }
        for fid, mid in samples.items():
            self.assertEqual(family_id("", mid, ""), fid,
                             f"{mid} 应归到 {fid}")

    def test_o1_o3_o4_series_matches_openai(self):
        """OpenAI 的 o1/o3/o4 系列（数字紧跟 o）要能认出来。"""
        for mid in ("o1-preview", "o3-mini", "o4-mini"):
            self.assertEqual(family_id("", mid, ""), "openai", mid)

    def test_provider_regex_matches_its_own_family(self):
        """每个家族自己的 provider 关键词，得能认出自己。

        注意：家族 id 不等于模型名关键词（openai 的 id 是 "openai"，
        但模型名关键词是 "gpt"），所以这里用各家族的真实模型名验证。
        """
        samples = {
            "openai": ("openai", "gpt-4o"),
            "claude": ("anthropic", "claude-sonnet-4"),
            "grok": ("xai", "grok-2"),
            "gemini": ("google", "gemini-2.0-pro"),
            "doubao": ("volcengine", "doubao-pro-32k"),
            "kimi": ("moonshot", "kimi-k2"),
            "deepseek": ("deepseek", "deepseek-chat"),
            "qwen": ("alibaba", "qwen-max"),
            "zhipu": ("zhipu", "glm-4-plus"),
        }
        for fam in FAMILIES:
            provider, model = samples[fam["id"]]
            self.assertIsNotNone(fam["provider_re"].search(provider),
                                 f"{fam['id']} 的 provider 正则认不出 {provider}")
            self.assertIsNotNone(fam["model_re"].search(model),
                                 f"{fam['id']} 的模型名正则认不出 {model}")


class TestEnabledPatterns(unittest.TestCase):
    """模型启用名单（Lancer 的 enabledModelPatterns）。"""

    def test_empty_pattern_means_all_enabled(self):
        self.assertTrue(is_model_enabled([], "ep1", "gpt-4o"))
        self.assertTrue(is_model_enabled(None, "ep1", "gpt-4o"))

    def test_exact_model_id(self):
        self.assertTrue(is_model_enabled(["deepseek-chat"], "ep1", "deepseek-chat"))
        self.assertFalse(is_model_enabled(["deepseek-chat"], "ep1", "gpt-4o"))

    def test_wildcard_pattern(self):
        self.assertTrue(is_model_enabled(["deepseek-*"], "ep1", "deepseek-chat"))
        self.assertFalse(is_model_enabled(["deepseek-*"], "ep1", "gpt-4o"))

    def test_endpoint_qualified_pattern(self):
        """provider/model 形式只匹配该端点下的模型。"""
        pats = ["ep_a/deepseek-chat"]
        self.assertTrue(is_model_enabled(pats, "ep_a", "deepseek-chat"))
        self.assertFalse(is_model_enabled(pats, "ep_b", "deepseek-chat"))

    def test_pattern_is_anchored(self):
        """不能前缀命中——'gpt' 不该匹配 'gpt-4o-mini' 之外的 'x-gpt-9'。"""
        self.assertFalse(is_model_enabled(["gpt"], "ep1", "xgpt9"))


class TestBuildCatalog(unittest.TestCase):

    def _eps(self):
        return [
            {"id": "ep_ds", "label": "DeepSeek", "baseUrl": "https://ds/v1",
             "apiKey": "sk-1", "provider": "deepseek",
             "models": ["deepseek-chat", "deepseek-reasoner"]},
            {"id": "ep_gw", "label": "自建网关", "baseUrl": "https://gw/v1",
             "apiKey": "sk-2", "provider": "openai",
             "models": ["claude-sonnet-4", "gpt-4o"]},
            {"id": "ep_nokey", "label": "没配Key", "baseUrl": "https://q/v1",
             "apiKey": "", "provider": "qwen", "models": ["qwen-max"]},
        ]

    def test_grouped_by_family(self):
        cat = build_catalog(self._eps())
        by_id = {f["id"]: f for f in cat["families"]}
        self.assertEqual(len(by_id["deepseek"]["models"]), 2)
        self.assertEqual(len(by_id["claude"]["models"]), 1)
        self.assertEqual(len(by_id["openai"]["models"]), 1)
        self.assertEqual(len(by_id["qwen"]["models"]), 1)

    def test_gateway_claude_grouped_under_claude_not_gpt(self):
        """网关转发 Claude → 归 Claude，不归 GPT。"""
        cat = build_catalog(self._eps())
        by_id = {f["id"]: f for f in cat["families"]}
        self.assertEqual(by_id["claude"]["models"][0]["providerId"], "ep_gw")
        self.assertEqual(by_id["claude"]["models"][0]["modelId"], "claude-sonnet-4")

    def test_three_states(self):
        cat = build_catalog(self._eps())
        rows = {(m["providerId"], m["modelId"]): m
                for f in cat["families"] for m in f["models"]}
        self.assertEqual(rows[("ep_ds", "deepseek-chat")]["state"],
                         STATE_AVAILABLE)
        # 没 Key 的端点 → 未配置
        self.assertEqual(rows[("ep_nokey", "qwen-max")]["state"],
                         STATE_UNCONFIGURED)

    def test_disabled_endpoint_makes_models_disabled(self):
        eps = self._eps()
        eps[0]["enabled"] = False
        cat = build_catalog(eps)
        rows = {(m["providerId"], m["modelId"]): m
                for f in cat["families"] for m in f["models"]}
        self.assertEqual(rows[("ep_ds", "deepseek-chat")]["state"],
                         STATE_DISABLED)

    def test_pattern_filters_model_to_disabled(self):
        """启用名单把模型过滤掉 → 未启用（不是未配置）。"""
        cat = build_catalog(self._eps(), ["gpt-*"])
        rows = {(m["providerId"], m["modelId"]): m
                for f in cat["families"] for m in f["models"]}
        self.assertEqual(rows[("ep_ds", "deepseek-chat")]["state"],
                         STATE_DISABLED)
        self.assertEqual(rows[("ep_gw", "gpt-4o")]["state"], STATE_AVAILABLE)

    def test_available_count_per_family(self):
        cat = build_catalog(self._eps())
        by_id = {f["id"]: f for f in cat["families"]}
        self.assertEqual(by_id["deepseek"]["count"], 2)
        self.assertEqual(by_id["qwen"]["count"], 0)
        self.assertEqual(by_id["qwen"]["modelCount"], 1)

    def test_unclassified_models_are_kept(self):
        """判不出家族的模型不能丢——用户自己知道那是什么。"""
        eps = [{"id": "ep_x", "label": "X", "baseUrl": "https://x/v1",
                "apiKey": "k", "models": ["mystery-model"]}]
        cat = build_catalog(eps)
        other = [f for f in cat["families"] if f["id"] == ""]
        self.assertEqual(len(other), 1)
        self.assertEqual(len(other[0]["models"]), 1)
        self.assertEqual(cat["totalModels"], 1)

    def test_active_is_reported(self):
        cat = build_catalog(self._eps(), [], "ep_gw", "claude-sonnet-4")
        self.assertEqual(cat["active"]["endpointId"], "ep_gw")
        self.assertEqual(cat["active"]["modelId"], "claude-sonnet-4")

    def test_endpoint_list_has_state_and_no_secret(self):
        cat = build_catalog(self._eps())
        for e in cat["endpoints"]:
            self.assertNotIn("apiKey", e)
            self.assertIn(e["state"], (STATE_AVAILABLE, STATE_DISABLED,
                                       STATE_UNCONFIGURED))

    def test_empty_input_does_not_crash(self):
        cat = build_catalog(None)
        self.assertEqual(cat["totalModels"], 0)
        self.assertEqual(cat["availableModels"], 0)
        self.assertIsNone(cat["active"])

    def test_duplicate_models_in_one_endpoint_deduped(self):
        eps = [{"id": "ep_a", "label": "A", "baseUrl": "https://a/v1",
                "apiKey": "k", "models": ["gpt-4o", "gpt-4o"]}]
        cat = build_catalog(eps)
        self.assertEqual(cat["totalModels"], 1)


class TestLlmConfigMultiEndpoint(unittest.TestCase):
    """多端点配置层 + 向后兼容。"""

    def setUp(self):
        # 每个用例一个独立的 MOSU_HOME，互不污染
        self._home = tempfile.mkdtemp(prefix="mosu_ep_test_")
        os.environ["MOSU_HOME"] = self._home
        # 强制重载 dataset，让 KEYVAULT 指向新的 home
        import importlib
        import core.dataset as ds
        importlib.reload(ds)
        import core.llmconfig as lc
        importlib.reload(lc)
        self.lc = lc
        self.ds = ds

    def tearDown(self):
        os.environ.pop("MOSU_HOME", None)

    def test_legacy_flat_config_still_works(self):
        """旧配置（只有 baseUrl/apiKey/model）必须继续可用。"""
        self.ds.save_keyvault({"llm": {
            "baseUrl": "https://api.deepseek.com/v1",
            "apiKey": "sk-legacy", "model": "deepseek-chat"}})
        cfg = self.lc.load_llm_config()
        self.assertEqual(cfg["baseUrl"], "https://api.deepseek.com/v1")
        self.assertEqual(cfg["model"], "deepseek-chat")
        self.assertTrue(cfg["hasKey"])

    def test_legacy_read_does_not_rewrite_user_file(self):
        """读操作不改写用户配置——合成端点只在内存里。"""
        self.ds.save_keyvault({"llm": {"baseUrl": "https://x/v1",
                                       "apiKey": "k", "model": "m"}})
        self.lc.load_endpoints()
        self.assertNotIn("endpoints", self.ds.load_keyvault()["llm"])

    def test_legacy_becomes_single_endpoint_in_catalog(self):
        self.ds.save_keyvault({"llm": {"baseUrl": "https://api.deepseek.com/v1",
                                       "apiKey": "sk-legacy",
                                       "model": "deepseek-chat"}})
        cat = self.lc.load_model_catalog()
        self.assertEqual(cat["totalModels"], 1)
        self.assertEqual(cat["availableModels"], 1)
        self.assertEqual(cat["active"]["modelId"], "deepseek-chat")

    def test_env_overrides_everything(self):
        """环境变量优先级最高——这是既有铁律，多端点不能破它。"""
        self.lc.save_endpoint({"id": "ep_a", "label": "A",
                               "baseUrl": "https://a/v1", "apiKey": "sk-a",
                               "models": ["gpt-4o"]})
        self.lc.set_active("ep_a", "gpt-4o")
        os.environ["MOSU_BOT_BASE_URL"] = "https://env/v1"
        os.environ["MOSU_BOT_MODEL"] = "env-model"
        try:
            cfg = self.lc.load_llm_config()
            self.assertEqual(cfg["baseUrl"], "https://env/v1")
            self.assertEqual(cfg["model"], "env-model")
            self.assertEqual(cfg["source"], "env")
        finally:
            os.environ.pop("MOSU_BOT_BASE_URL", None)
            os.environ.pop("MOSU_BOT_MODEL", None)

    def test_save_and_activate_endpoint(self):
        self.lc.save_endpoint({"id": "ep_a", "label": "A",
                               "baseUrl": "https://a/v1", "apiKey": "sk-a",
                               "models": ["gpt-4o"]})
        self.lc.save_endpoint({"id": "ep_b", "label": "B",
                               "baseUrl": "https://b/v1", "apiKey": "sk-b",
                               "models": ["claude-sonnet-4"]})
        self.lc.set_active("ep_b", "claude-sonnet-4")
        cfg = self.lc.load_llm_config()
        self.assertEqual(cfg["baseUrl"], "https://b/v1")
        self.assertEqual(cfg["model"], "claude-sonnet-4")
        self.assertEqual(cfg["source"], "active")

    def test_activate_writes_back_flat_fields(self):
        """回写扁平字段是向后兼容的关键——老代码读的是它。"""
        self.lc.save_endpoint({"id": "ep_a", "label": "A",
                               "baseUrl": "https://a/v1", "apiKey": "sk-a",
                               "models": ["gpt-4o"]})
        self.lc.set_active("ep_a", "gpt-4o")
        raw = self.ds.load_keyvault()["llm"]
        self.assertEqual(raw["baseUrl"], "https://a/v1")
        self.assertEqual(raw["apiKey"], "sk-a")
        self.assertEqual(raw["model"], "gpt-4o")

    def test_activate_records_active_model_in_catalog(self):
        """目录里的 active 必须指向刚切换的模型。

        注意：这条跟扁平字段的 model 是**两条独立的数据**——
        扁平 model 来自 cfg["model"] 回写，目录 active 来自 cfg["activeModel"]。
        只测前者的话，activeModel 被破坏时测试照样全绿（反向验证暴露过）。
        """
        self.lc.save_endpoint({"id": "ep_a", "label": "A",
                               "baseUrl": "https://a/v1", "apiKey": "sk-a",
                               "models": ["gpt-4o", "claude-sonnet-4"]})
        self.lc.save_endpoint({"id": "ep_b", "label": "B",
                               "baseUrl": "https://b/v1", "apiKey": "sk-b",
                               "models": ["deepseek-chat"]})
        self.lc.set_active("ep_b", "deepseek-chat")
        cat = self.lc.load_model_catalog()
        self.assertEqual(cat["active"]["endpointId"], "ep_b")
        self.assertEqual(cat["active"]["modelId"], "deepseek-chat")

    def test_activate_marks_the_right_endpoint_active(self):
        """端点列表里只有被选中的那个标 active。"""
        self.lc.save_endpoint({"id": "ep_a", "label": "A",
                               "baseUrl": "https://a/v1", "apiKey": "k",
                               "models": ["gpt-4o"]})
        self.lc.save_endpoint({"id": "ep_b", "label": "B",
                               "baseUrl": "https://b/v1", "apiKey": "k",
                               "models": ["gpt-4o"]})
        self.lc.set_active("ep_b", "gpt-4o")
        cat = self.lc.load_model_catalog()
        active_eps = [e["id"] for e in cat["endpoints"] if e["active"]]
        self.assertEqual(active_eps, ["ep_b"])

    def test_updating_endpoint_without_key_keeps_old_key(self):
        """前端回填的是掩码，传空串 = 不改动，不能把真 Key 抹掉。"""
        self.lc.save_endpoint({"id": "ep_a", "label": "A",
                               "baseUrl": "https://a/v1", "apiKey": "sk-a",
                               "models": ["gpt-4o"]})
        self.lc.save_endpoint({"id": "ep_a", "label": "A2",
                               "baseUrl": "https://a2/v1", "apiKey": "",
                               "models": ["gpt-4o"]})
        eps = self.lc.load_endpoints()
        self.assertEqual(eps[0]["apiKey"], "sk-a")
        self.assertEqual(eps[0]["baseUrl"], "https://a2/v1")

    def test_delete_active_endpoint_clears_active(self):
        self.lc.save_endpoint({"id": "ep_a", "label": "A",
                               "baseUrl": "https://a/v1", "apiKey": "k",
                               "models": ["gpt-4o"]})
        self.lc.set_active("ep_a", "gpt-4o")
        self.lc.delete_endpoint("ep_a")
        cfg = self.lc.load_llm_config()
        self.assertEqual(cfg["activeEndpoint"], "")

    def test_endpoint_without_id_gets_generated(self):
        eps = self.lc.save_endpoint({"label": "X", "baseUrl": "https://x/v1",
                                     "apiKey": "k", "models": ["gpt-4o"]})
        self.assertTrue(eps[0]["id"].startswith("ep_"))

    def test_enabled_models_persist(self):
        self.lc.set_enabled_models(["deepseek-*"])
        self.assertEqual(self.lc.load_keyvault_enabled()
                         if hasattr(self.lc, "load_keyvault_enabled")
                         else self.ds.load_keyvault()["llm"]["enabledModels"],
                         ["deepseek-*"])

    def test_activate_unknown_endpoint_raises(self):
        with self.assertRaises(ValueError):
            self.lc.set_active("nope", "gpt-4o")


if __name__ == "__main__":
    unittest.main()
