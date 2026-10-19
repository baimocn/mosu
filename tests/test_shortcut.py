# -*- coding: utf-8 -*-
"""桌面快捷方式：图标降级路径必须都不硬失败。

这是 D3 的核心不变量：**建入口的退出码必须是 0**。
图标是观感问题，缺 Pillow / 缺 PNG / home 只读都不该让用户建不出桌面入口——
一旦这里抛异常，用户看到的就是"装个图标把快捷方式搞挂了"。

四级场景都覆盖：
1. 有 Pillow + PNG → mac 生成 icns，Linux 写绝对路径 Icon=
2. 无 Pillow + 有 PNG → mac 只出 .command（无 icns），**Linux 仍设 PNG 图标**
3. 两者皆无 → 两平台都不设图标，但 .desktop 仍合法（不含 Icon= 键）
4. Linux home 只读 → Icon=mosu 主题名回退
外加：重复执行不产生重复文件。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class _Proxy:
    """模块替身：只覆盖给定属性，其余透传。

    直接 patch `os.name` / `sys.platform` 会改到**全局**模块对象，
    同一进程里的其他代码（pathlib、tempfile…）会跟着看到假平台。
    这里换成替身对象，作用域只限于 core.shortcut。
    """

    def __init__(self, real, **over):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_over", over)

    def __getattr__(self, k):
        over = object.__getattribute__(self, "_over")
        if k in over:
            return over[k]
        return getattr(object.__getattribute__(self, "_real"), k)


def _fake_home(tmp: str) -> Path:
    h = Path(tmp)
    (h / "Desktop").mkdir(parents=True, exist_ok=True)
    return h


# 先抓住真函数引用。setUp 里会把 core.shortcut.ensure_icns 掐掉（防写脏仓库），
# 到需要验证生成的用例再换回来——若在换回时才 import，拿到的会是已被替换的替身。
from core.shortcut import ensure_icns as _real_ensure_icns


class ShortcutTestBase(unittest.TestCase):
    """把 home / 平台 / 图标可用性全部换成可控替身，绝不碰真实桌面。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mosu-shortcut-")
        self.home = _fake_home(self.tmp)
        p = patch.object(Path, "home", classmethod(lambda cls: self.home))
        p.start()
        self.addCleanup(p.stop)
        # 默认掐掉 icns 生成：否则 create_shortcut() 会把产物写进仓库的 gui/assets/
        p = patch("core.shortcut.ensure_icns", lambda: False)
        p.start()
        self.addCleanup(p.stop)

    def set_platform(self, value: str):
        """伪装成 posix 平台 + 指定 sys.platform（Windows 分支随之失效）。"""
        for mod, proxy in (("core.shortcut.os", _Proxy(os, name="posix")),
                           ("core.shortcut.sys", _Proxy(sys, platform=value))):
            p = patch(mod, proxy)
            p.start()
            self.addCleanup(p.stop)

    def no_icons(self):
        """模拟两个图标文件都不存在。"""
        p = patch("core.shortcut._resolve_icons", lambda: {"png": None, "icns": None})
        p.start()
        self.addCleanup(p.stop)

    def png_only(self, tmp_png: Path | None = None):
        """只有 PNG、没有 icns（无 Pillow 的 mac 场景）。"""
        png = tmp_png
        if png is None:
            png = Path(self.tmp) / "logo.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n")
        p = patch("core.shortcut._resolve_icons",
                  lambda: {"png": png, "icns": None})
        p.start()
        self.addCleanup(p.stop)
        return png

    def desktop_file(self) -> Path:
        return self.home / "Desktop" / "墨溯.desktop"


class TestLinuxDesktop(ShortcutTestBase):
    def setUp(self):
        super().setUp()
        self.set_platform("linux")

    def test_png_installed_writes_absolute_icon_path(self):
        png = self.png_only()
        from core import shortcut as S
        made = S._shortcut_posix()
        self.assertTrue(made)
        text = self.desktop_file().read_text(encoding="utf-8")
        self.assertTrue(text.startswith("[Desktop Entry]"))
        self.assertIn(f"Icon={self.home / S.LINUX_ICON_REL / 'mosu.png'}", text)
        # 图标确实被复制过去了
        self.assertTrue((self.home / S.LINUX_ICON_REL / "mosu.png").exists())
        self.assertEqual((self.home / S.LINUX_ICON_REL / "mosu.png").read_bytes(),
                         png.read_bytes())

    def test_no_png_omits_icon_key_but_file_stays_valid(self):
        """无 PNG 时**整行 Icon= 都不写**——写空值会让部分桌面环境解析失败。"""
        self.no_icons()
        from core import shortcut as S
        self.assertEqual(S.create_shortcut(), 0)
        text = self.desktop_file().read_text(encoding="utf-8")
        self.assertTrue(text.startswith("[Desktop Entry]"))
        self.assertNotIn("Icon=", text)
        # 必须仍是合法的 freedesktop 入口
        for key in ("Type=Application", "Name=墨溯", "Exec=", "Categories=Utility;"):
            self.assertIn(key, text)

    def test_install_failure_falls_back_to_theme_name(self):
        """装不上图标（home 只读）→ 回退主题名，仍是合法 Icon= 值。"""
        self.png_only()

        def boom(*a, **k):
            raise PermissionError("read-only file system")

        from core import shortcut as S
        with patch("core.shortcut.shutil.copyfile", boom):
            self.assertEqual(S.create_shortcut(), 0)
        text = self.desktop_file().read_text(encoding="utf-8")
        self.assertIn("Icon=mosu", text)
        self.assertTrue(text.startswith("[Desktop Entry]"))

    def test_mkdir_failure_falls_back_to_theme_name(self):
        """图标目录建不出来（home 只读）→ 同样是主题名回退，不是崩溃。"""
        self.png_only()

        class _NoMkdir(type(Path())):
            """只有图标目录建不出来；桌面目录照常（模拟 home 部分只读）。"""

            def mkdir(self, *a, **k):
                if "icons" in str(self):
                    raise OSError("read-only file system")
                return super().mkdir(*a, **k)

            @classmethod
            def home(cls):
                return cls(super().home())

        from core import shortcut as S
        # 只替换 core.shortcut 里的 Path 名字，不动全局 pathlib
        p = patch("core.shortcut.Path", _NoMkdir)
        p.start()
        self.addCleanup(p.stop)
        self.assertEqual(S.create_shortcut(), 0)
        self.assertIn("Icon=mosu", self.desktop_file().read_text(encoding="utf-8"))

    def test_repeat_creates_no_duplicates(self):
        """重复执行必须幂等：同名覆盖，不产生 "墨溯 (1).desktop" 之类。"""
        self.png_only()
        from core import shortcut as S
        self.assertEqual(S.create_shortcut(), 0)
        first = self.desktop_file().read_text(encoding="utf-8")
        self.assertEqual(S.create_shortcut(), 0)
        self.assertEqual(S.create_shortcut(), 0)
        self.assertEqual(self.desktop_file().read_text(encoding="utf-8"), first)
        entries = list((self.home / "Desktop").iterdir())
        self.assertEqual([p.name for p in entries], ["墨溯.desktop"])


class TestMacShortcut(ShortcutTestBase):
    def setUp(self):
        super().setUp()
        self.set_platform("darwin")

    def test_icns_copied_to_desktop_when_available(self):
        from core import shortcut as S
        icns = Path(self.tmp) / "mosu.icns"
        icns.write_bytes(b"icns-bytes")
        p = patch("core.shortcut._resolve_icons",
                  lambda: {"png": Path(self.tmp) / "x.png", "icns": icns})
        p.start()
        self.addCleanup(p.stop)
        p2 = patch.object(S, "ensure_icns", lambda: True)
        p2.start()
        self.addCleanup(p2.stop)

        self.assertEqual(S.create_shortcut(), 0)
        desk = self.home / "Desktop"
        self.assertTrue((desk / "墨溯.command").exists())
        self.assertTrue((desk / "墨溯.icns").exists())
        self.assertEqual((desk / "墨溯.icns").read_bytes(), b"icns-bytes")

    def test_no_icns_still_exits_zero(self):
        """无 Pillow 时 mac 只出 .command——入口可用，退出码必须是 0。"""
        self.png_only()
        from core import shortcut as S
        p = patch.object(S, "ensure_icns", lambda: False)
        p.start()
        self.addCleanup(p.stop)
        self.assertEqual(S.create_shortcut(), 0)
        self.assertTrue((self.home / "Desktop" / "墨溯.command").exists())
        self.assertFalse((self.home / "Desktop" / "墨溯.icns").exists())

    def test_command_is_executable(self):
        self.png_only()
        from core import shortcut as S
        S.create_shortcut()
        f = self.home / "Desktop" / "墨溯.command"
        self.assertTrue(os.access(f, os.X_OK), ".command 必须可执行，否则双击没反应")
        self.assertIn("#!/bin/sh", f.read_text(encoding="utf-8"))


class TestNoIconsAnywhere(ShortcutTestBase):
    def test_both_platforms_exit_zero_without_icons(self):
        from core import shortcut as S
        self.no_icons()
        for plat in ("linux", "darwin"):
            with self.subTest(platform=plat):
                self.set_platform(plat)
                p = patch.object(S, "ensure_icns", lambda: False)
                p.start()
                self.addCleanup(p.stop)
                self.assertEqual(S.create_shortcut(), 0)


class TestEnsureIcns(ShortcutTestBase):
    def test_missing_png_returns_false_without_raising(self):
        from core import shortcut as S
        p = patch.object(S, "ICON_PNG", Path(self.tmp) / "nope.png")
        p.start()
        self.addCleanup(p.stop)
        self.assertIs(S.ensure_icns(), False)

    def test_missing_pillow_returns_false_without_raising(self):
        """Pillow 是可选依赖，缺它必须安静降级而不是抛 ImportError。"""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("no PIL")
            return real_import(name, *a, **k)

        from core import shortcut as S
        src = Path(self.tmp) / "logo-512.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\n")
        p = patch.object(S, "ICON_PNG", src)
        p.start()
        self.addCleanup(p.stop)
        with patch("builtins.__import__", fake_import):
            self.assertIs(S.ensure_icns(), False)

    def setUp(self):
        super().setUp()
        # 生成路径必须用真的 ensure_icns（setUp 里默认掐掉了）
        p = patch("core.shortcut.ensure_icns", _real_ensure_icns)
        p.start()
        self.addCleanup(p.stop)

    def test_pillow_writes_icns(self):
        try:
            import PIL  # noqa: F401
        except Exception:
            self.skipTest("本机无 Pillow，跳过生成路径")
        from PIL import Image
        from core import shortcut as S
        src = self.home / "logo.png"
        Image.new("RGBA", (64, 64), (10, 200, 100, 255)).save(src)
        out = Path(self.tmp) / "out.icns"      # 写进 tmp，绝不落在仓库里
        with patch.object(S, "ICON_PNG", src), patch.object(S, "ICON_ICNS", out):
            self.assertIs(S.ensure_icns(), True)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 0)

    def test_create_shortcut_generates_icns_on_posix(self):
        """mac 分支建入口前必须先生成 icns，否则桌面永远没图标。"""
        try:
            import PIL  # noqa: F401
        except Exception:
            self.skipTest("本机无 Pillow，跳过生成路径")
        self.set_platform("darwin")
        from core import shortcut as S
        src = self.home / "logo.png"
        from PIL import Image
        Image.new("RGBA", (64, 64), (10, 200, 100, 255)).save(src)
        out = Path(self.tmp) / "generated.icns"
        with patch.object(S, "ICON_PNG", src), patch.object(S, "ICON_ICNS", out):
            self.assertEqual(S.create_shortcut(), 0)
            self.assertTrue(out.exists(), "建入口前应先生成 icns")
        self.assertTrue((self.home / "Desktop" / "墨溯.icns").exists())


class TestRemoveShortcut(ShortcutTestBase):
    def test_linux_removes_desktop_entry_only(self):
        from core import shortcut as S
        self.png_only()
        self.set_platform("linux")
        S.create_shortcut()
        installed = self.home / S.LINUX_ICON_REL / "mosu.png"
        self.assertTrue(installed.exists())

        self.assertEqual(S.remove_shortcut(), 0)
        self.assertFalse(self.desktop_file().exists())
        # 共享图标目录里的那份必须留着——别的 .desktop 可能正引用它
        self.assertTrue(installed.exists(), "共享图标目录不该被 remove 扫掉")

    def test_mac_removes_command_and_desktop_icns(self):
        from core import shortcut as S
        icns = Path(self.tmp) / "mosu.icns"
        icns.write_bytes(b"icns")
        self.set_platform("darwin")
        p = patch("core.shortcut._resolve_icons",
                  lambda: {"png": None, "icns": icns})
        p.start()
        self.addCleanup(p.stop)
        p2 = patch.object(S, "ensure_icns", lambda: True)
        p2.start()
        self.addCleanup(p2.stop)
        S.create_shortcut()
        self.assertEqual(S.remove_shortcut(), 0)
        desk = self.home / "Desktop"
        self.assertFalse((desk / "墨溯.command").exists())
        self.assertFalse((desk / "墨溯.icns").exists())

    def test_remove_is_idempotent(self):
        from core import shortcut as S
        self.set_platform("linux")
        self.assertEqual(S.remove_shortcut(), 0)
        self.assertEqual(S.remove_shortcut(), 0)


class TestIconConstants(ShortcutTestBase):
    def test_png_source_ships_with_repo(self):
        """logo-512.png 是既成资产，被误删会让三个平台一起丢图标。"""
        from core import shortcut as S
        self.assertTrue(S.ICON_PNG.exists(),
                        f"{S.ICON_PNG} 不存在，Linux 快捷方式将永远无图标")
        self.assertEqual(S.ICON_PNG.name, "logo-512.png")

    def test_resolve_icons_returns_both_keys(self):
        from core import shortcut as S
        icons = S._resolve_icons()
        self.assertEqual(set(icons), {"png", "icns"})
        self.assertTrue(icons["png"] is not None)


if __name__ == "__main__":
    unittest.main()
