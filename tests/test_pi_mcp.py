# -*- coding: utf-8 -*-
"""Pi harness 接入的防腐化测试。

背景（2026-10-31）：把墨溯挂到 Pi 上当 MCP server 时，连通性踩了三个坑，
每一个的报错信息都**不指向真正的原因**，只能靠抓包 + 二分定位：

1. `role: "developer"` → 上游渠道校验拒绝，报
   "Illegal API invocation from an unapproved channel"
   （看着像账号/权限问题，其实是消息角色不认）
2. `api: "openai-responses"` → 网关只有 /v1/chat/completions，404
3. `authHeader` 默认 false → 不发 Authorization: Bearer，网关认不出渠道

这三个坑任何一个复现，Pi 都是「静默挂起」或含糊报错，用户根本无从下手。
所以这里锁死配置：**改了 models.json 的兼容字段，本测试会红**。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import install_pi_mcp as ipm  # noqa: E402


class TestPiMcpEntry(unittest.TestCase):
    """MCP 条目本身的形状。"""

    def test_entry_points_to_real_server(self):
        e = ipm.mcp_entry()
        self.assertTrue(Path(e["command"]).exists(), "python 解释器路径不存在")
        self.assertTrue(Path(e["args"][0]).exists(), "mcp_server.py 不存在")
        self.assertTrue(Path(e["args"][0]).name == "mcp_server.py")

    def test_entry_exposure_is_direct(self):
        """必须是 direct。

        Pi 的 MCP 默认 exposure 是 codemode —— 工具**不直接给模型看**，
        模型得写 codemode 脚本间接调。用户要的是「agent 直接调用分析」，
        必须是 direct。
        """
        self.assertEqual(ipm.mcp_entry().get("exposure"), "direct")

    def test_entry_has_description(self):
        """description 会被列进 Pi 的系统提示，也是 tool_search 的排序依据。"""
        self.assertTrue(ipm.mcp_entry().get("description"))

    def test_entry_sets_utf8_env(self):
        """不设 PYTHONIOENCODING=utf-8，Windows 下中文会炸。"""
        self.assertEqual(ipm.mcp_entry()["env"].get("PYTHONIOENCODING"), "utf-8")


class TestPiProviderFixes(unittest.TestCase):
    """三个坑的修复值不能被改回去。"""

    def test_api_is_chat_completions(self):
        """网关只有 /v1/chat/completions；openai-responses 会 404。"""
        self.assertEqual(ipm.models_fix()["api"], "openai-completions")

    def test_auth_header_enabled(self):
        """Pi 默认不发 Authorization: Bearer（authHeader 默认 false）。"""
        self.assertIs(ipm.models_fix()["authHeader"], True)

    def test_developer_role_disabled(self):
        """网关上游不认 role=developer，必须退回 system。"""
        self.assertIs(ipm.models_fix()["compat"]["supportsDeveloperRole"], False)


class TestRealPiConfig(unittest.TestCase):
    """本机真实配置也必须是对的（不能只测脚本常量）。"""

    def _models(self):
        p = Path.home() / ".pi" / "agent" / "models.json"
        if not p.exists():
            self.skipTest("Pi 还没配过 provider")
        return json.loads(p.read_text(encoding="utf-8"))

    def test_gateway_provider_is_compatible(self):
        want = ipm.models_fix()
        for name, prov in (self._models().get("providers") or {}).items():
            if not isinstance(prov, dict):
                continue
            if "8787" not in str(prov.get("baseUrl") or ""):
                continue
            for k, v in want.items():
                self.assertEqual(prov.get(k), v,
                                 f"provider {name} 的 {k} 不对——Pi 会连不上 8787 网关")

    def test_real_mcp_json_has_mosu(self):
        p = Path.home() / ".pi" / "agent" / "mcp.json"
        if not p.exists():
            self.skipTest("Pi 还没配过 MCP")
        cfg = json.loads(p.read_text(encoding="utf-8"))
        entry = (cfg.get("mcpServers") or {}).get("mosu")
        self.assertIsNotNone(entry, "mcp.json 里没有 mosu 条目")
        self.assertEqual(entry.get("exposure"), "direct")


if __name__ == "__main__":
    unittest.main()
