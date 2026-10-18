# -*- coding: utf-8 -*-
"""多账号测试：旧格式自动迁移、解析链优先级、账号增删切换（全部用临时目录）。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.dataset as ds_mod  # noqa: E402

KEY_A = "aa" * 32
KEY_B = "bb" * 32


class AccountTestCase(unittest.TestCase):
    """每个用例一套临时密钥库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "config.json"
        self._patch = mock.patch.object(ds_mod, "KEYVAULT", self.vault)
        self._patch.start()
        for k in ("MOSU_ACCOUNT",):
            os.environ.pop(k, None)

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def write_flat(self, **kw):
        data = {"dbPath": "D:/wechat/a", "keyHex": KEY_A, "wxid": "wxid_a"}
        data.update(kw)
        self.vault.parent.mkdir(parents=True, exist_ok=True)
        self.vault.write_text(json.dumps(data), encoding="utf-8")

    def write_accounts(self, accounts, current=None):
        payload = {"accounts": accounts}
        if current:
            payload["current"] = current
        self.vault.parent.mkdir(parents=True, exist_ok=True)
        self.vault.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestMigration(AccountTestCase):
    def test_flat_auto_migrates(self):
        self.write_flat()
        accounts, current = ds_mod.load_accounts()
        self.assertEqual(list(accounts), ["wxid_a"])
        self.assertEqual(current, "wxid_a")
        self.assertEqual(accounts["wxid_a"]["keyHex"], KEY_A)
        # 迁移结果已落盘
        on_disk = json.loads(self.vault.read_text(encoding="utf-8"))
        self.assertIn("accounts", on_disk)
        self.assertNotIn("keyHex", on_disk)          # 旧扁平键已清

    def test_flat_without_wxid_uses_default(self):
        self.write_flat(wxid=None)
        data = json.loads(self.vault.read_text(encoding="utf-8"))
        data.pop("wxid")
        self.vault.write_text(json.dumps(data), encoding="utf-8")
        accounts, current = ds_mod.load_accounts()
        self.assertEqual(current, "default")

    def test_empty_vault(self):
        accounts, current = ds_mod.load_accounts()
        self.assertEqual((accounts, current), ({}, None))


class TestResolve(AccountTestCase):
    def test_single_account_no_name_needed(self):
        self.write_accounts({"wxid_a": {"keyHex": KEY_A}}, current="wxid_a")
        name, acc = ds_mod.resolve_account()
        self.assertEqual(name, "wxid_a")

    def test_explicit_wins(self):
        self.write_accounts({"a": {"keyHex": KEY_A}, "b": {"keyHex": KEY_B}}, current="a")
        name, acc = ds_mod.resolve_account("b")
        self.assertEqual((name, acc["keyHex"]), ("b", KEY_B))

    def test_env_second(self):
        self.write_accounts({"a": {"keyHex": KEY_A}, "b": {"keyHex": KEY_B}}, current="a")
        os.environ["MOSU_ACCOUNT"] = "b"
        try:
            name, _ = ds_mod.resolve_account()
            self.assertEqual(name, "b")
        finally:
            os.environ.pop("MOSU_ACCOUNT", None)

    def test_current_third(self):
        self.write_accounts({"a": {"keyHex": KEY_A}, "b": {"keyHex": KEY_B}}, current="b")
        name, _ = ds_mod.resolve_account()
        self.assertEqual(name, "b")

    def test_unknown_name_raises(self):
        self.write_accounts({"a": {"keyHex": KEY_A}}, current="a")
        with self.assertRaises(SystemExit):
            ds_mod.resolve_account("nope")

    def test_multi_without_current_raises(self):
        self.write_accounts({"a": {"keyHex": KEY_A}, "b": {"keyHex": KEY_B}})
        with self.assertRaises(SystemExit):
            ds_mod.resolve_account()

    def test_load_key_hex_by_account(self):
        self.write_accounts({"a": {"keyHex": KEY_A}, "b": {"keyHex": KEY_B}}, current="a")
        self.assertEqual(ds_mod.load_key_hex("b"), KEY_B)
        self.assertEqual(ds_mod.load_key_hex(), KEY_A)

    def test_env_key_overrides_account(self):
        self.write_accounts({"a": {"keyHex": KEY_A}}, current="a")
        os.environ["MOSU_KEY_HEX"] = KEY_B
        try:
            self.assertEqual(ds_mod.load_key_hex(), KEY_B)
        finally:
            os.environ.pop("MOSU_KEY_HEX", None)


class TestSaveRemove(AccountTestCase):
    def test_roundtrip(self):
        ds_mod.save_accounts({"a": {"keyHex": KEY_A, "dbPath": "D:/x"}}, current="a")
        accounts, current = ds_mod.load_accounts()
        self.assertEqual(current, "a")
        self.assertEqual(accounts["a"]["dbPath"], "D:/x")

    def test_save_keeps_extra_toplevel(self):
        self.write_flat()
        ds_mod.load_accounts()                       # 先迁移
        self.vault.write_text(json.dumps(
            {"accounts": {"a": {"keyHex": KEY_A}}, "current": "a", "defaultLimit": 50},
            ensure_ascii=False), encoding="utf-8")
        ds_mod.save_accounts({"a": {"keyHex": KEY_A}}, current="a")
        data = json.loads(self.vault.read_text(encoding="utf-8"))
        self.assertEqual(data["defaultLimit"], 50)

    def test_remove_current_falls_back(self):
        ds_mod.save_accounts({"a": {"keyHex": KEY_A}, "b": {"keyHex": KEY_B}}, current="a")
        accounts, current = ds_mod.load_accounts()
        del accounts["a"]
        ds_mod.save_accounts(accounts, current=next(iter(accounts), None))
        _, current2 = ds_mod.load_accounts()
        self.assertEqual(current2, "b")


class TestFindUserDir(AccountTestCase):
    def test_account_dbpath_used(self):
        with tempfile.TemporaryDirectory() as td:
            acc = Path(td) / "wxid_x"
            (acc / "db_storage").mkdir(parents=True)
            self.write_accounts({"wxid_x": {"keyHex": KEY_A, "dbPath": str(acc)}},
                                current="wxid_x")
            self.assertEqual(ds_mod.find_user_dir(), acc)

    def test_missing_dbpath_raises_not_silent(self):
        """账号配了目录却不存在 → 必须报错，不能静默读到别的账号数据。"""
        self.write_accounts({"wxid_x": {"keyHex": KEY_A, "dbPath": "D:/nope/x"}},
                            current="wxid_x")
        with self.assertRaises(SystemExit):
            ds_mod.find_user_dir()

    def test_explicit_beats_account(self):
        with tempfile.TemporaryDirectory() as td:
            other = Path(td) / "other"
            (other / "db_storage").mkdir(parents=True)
            self.write_accounts({"wxid_x": {"keyHex": KEY_A, "dbPath": "D:/nope/x"}},
                                current="wxid_x")
            self.assertEqual(ds_mod.find_user_dir(other), other)


if __name__ == "__main__":
    unittest.main()
