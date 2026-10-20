# -*- coding: utf-8 -*-
"""前端两张中文映射表的不变量（纯 JS 逻辑，用 node 跑）。

为什么要有：这两张表是「后端值域 → 中文文案」的唯一翻译层，出错时
**不报错、不白屏、只是默默显示错的名字** —— 截图评审和用户都可能当成
「这功能就这样」而不会报 bug。

历史缺陷（两轮叠加）：
1. 后端 `contact_type()`（5 值）与 `_session_type()`（3 值）曾共用一张表，
   `/api/sessions` 返回的 `private` 被翻成「好友」——而它在通讯录页确实是
   好友，于是同一个会话在两个页面显示不同类型。
2. `_session_type` 只判 `gh_` 前缀，漏了公众号的 `wxid_gh` 写法，
   同一账号在浏览页显示「单聊」、联系人页显示「公众号」。

两套词表**刻意不合并**：通讯录看 `delete_flag`（有「已删除」这一档），
会话看 `session_type`（没有），合并会丢信息。合并的诱惑会一直存在，
所以这里把「两张表必须都在、且 private 不许出现在会话表里」锁死。
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWS_DATA = ROOT / "gui" / "assets" / "views-data.js"
VIEWS_INSIGHT = ROOT / "gui" / "assets" / "views-insight.js"

NODE = shutil.which("node")
# Windows 上 node 常不在 PATH。只在测试环境里退回到常见安装位置，
# 不硬编码个人目录——仓库是公开的。
if not NODE:
    _cands = [os.environ.get("NODE_HOME") or ""]
    _cands += [os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs"),
               os.path.expandvars(r"%ProgramFiles%\nodejs"),
               os.path.expandvars(r"%ProgramFiles(x86)%\nodejs")]
    for _cand in filter(None, _cands):
        _hits = sorted(Path(_cand).glob("**/node.exe"))
        if _hits:
            NODE = str(_hits[-1])
            break


# 一次性把两个组件方法从 views-data.js 里取出来跑的沙箱脚本。
# 刻意**加载真实文件**而不是复刻逻辑：复刻的版本会与代码脱节而永远通过。
_SANDBOX = r"""
const fs = require('fs'), vm = require('vm');
const win = {
  Mosu: { fmtNum: String, initial: () => '', highlight: (t) => t,
          avatarColor: () => '#333', toast: {}, api: {} },
  MosuViews: { Lightbox: {}, JsonView: {} },
};
win.window = win; win.globalThis = win; win.console = console;
win.setTimeout = setTimeout; win.encodeURIComponent = encodeURIComponent;
win.document = { createElement: () => ({ click() {} }) };
win.location = { hash: '' };
vm.createContext(win);
// 路径走环境变量：node -e 模式下 argv 的下标随参数个数变化，
// 用 argv[2] 会在某些调用方式下拿到 undefined。
vm.runInContext(fs.readFileSync(process.env.MOSU_VIEWS_DATA, 'utf8'), win);
const C = win.MosuViews.Contacts;
console.log(JSON.stringify({
  session:  ['private', 'group', 'mp', 'former_friend', 'other', '', 'weird']
              .map((t) => C.methods.sessionTypeName(t)),
  contact:  ['friend', 'group', 'mp', 'former_friend', 'other', 'private', '', 'weird']
              .map((t) => C.methods.contactTypeName(t)),
  exported: typeof win.MosuViews.sessionTypeName,
}));
"""


def _node():
    if not NODE or not Path(NODE).exists():
        raise unittest.SkipTest("本机找不到 node，跳过前端映射表断言")
    return NODE


@unittest.skipUnless(NODE, "需要 node 才能验证前端 JS")
class TestTypeNameTables(unittest.TestCase):
    """两张映射表的正确性。两表都从真实组件方法取出，不复刻。"""

    @classmethod
    def setUpClass(cls):
        import os
        env = dict(os.environ, MOSU_VIEWS_DATA=str(VIEWS_DATA))
        r = subprocess.run(
            [_node(), "-e", _SANDBOX],
            capture_output=True, text=True, timeout=60, cwd=str(ROOT), env=env,
        )
        if r.returncode != 0:
            raise AssertionError(f"加载 views-data.js 失败：\n{r.stderr[:800]}")
        cls.out = json.loads(r.stdout.strip().splitlines()[-1])
        cls.session = dict(zip(
            ["private", "group", "mp", "former_friend", "other", "", "weird"],
            cls.out["session"]))
        cls.contact = dict(zip(
            ["friend", "group", "mp", "former_friend", "other", "private", "", "weird"],
            cls.out["contact"]))

    # ------------------------------------------------------------ 会话词表
    def test_session_private_is_单聊_not_好友(self):
        """核心回归：`_session_type` 的单聊值是 private。
        旧代码把它翻成「好友」，于是同一个会话在浏览页与联系人页类型不同。"""
        self.assertEqual(
            self.session["private"], "单聊",
            "会话表的 private 必须译作「单聊」；译成「好友」会让同一账号"
            "在两个页面显示不同类型")

    def test_session_table_covers_exact_backend_domain(self):
        """后端 `_session_type` 的值域精确是 {private, group, mp}。"""
        for t in ("private", "group", "mp"):
            self.assertNotIn(
                self.session[t], ("—", t),
                f"会话表的 {t} 未被翻译，会直接把英文原值显示给用户")

    def test_session_table_has_no_contact_only_entries(self):
        """`former_friend`（已删除）只存在于通讯录口径，会话表不该有。
        两表混用正是本文件要防的那个 bug 的根源。"""
        self.assertEqual(
            self.session["former_friend"], "former_friend",
            "会话表混入了通讯录专属值 former_friend，两表被合并了")

    # ---------------------------------------------------------- 通讯录词表
    def test_contact_table_covers_exact_backend_domain(self):
        """后端 `contact_type()` 的值域是
        {group, mp, friend, former_friend, other}。"""
        for t in ("group", "mp", "friend", "former_friend", "other"):
            self.assertNotIn(
                self.contact[t], ("—", t),
                f"通讯录表的 {t} 未被翻译，会直接把英文原值显示给用户")

    def test_contact_table_has_no_session_only_entries(self):
        """`private` 只属于会话口径。通讯录里出现它说明两表被合并了。"""
        self.assertEqual(
            self.contact["private"], "private",
            "通讯录表混入了会话专属值 private，两表被合并了")

    # -------------------------------------------------------------- 健壮性
    def test_empty_and_unknown_fall_back_gracefully(self):
        """空值显示破折号，未知值回退原值——都不该抛错或显示 undefined。"""
        for table in (self.session, self.contact):
            self.assertEqual(table[""], "—")
            self.assertEqual(table["weird"], "weird")
            for v in table.values():
                self.assertNotIn("undefined", str(v))

    def test_session_table_exported_for_reuse(self):
        """views-insight.js 靠 MosuViews.sessionTypeName 复用同一张表。
        导出没了它就会静默变成 undefined → 画像页会话下拉全是 undefined。"""
        self.assertEqual(self.out["exported"], "function")

    def test_no_legacy_shared_type_name_left(self):
        """旧的单一 typeName 必须彻底消失。
        它还留着就说明有人只改了新增处、没删旧调用点。"""
        for f in (VIEWS_DATA, VIEWS_INSIGHT):
            src = f.read_text(encoding="utf-8")
            body = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
            self.assertNotRegex(
                body, r"typeName\s*\(\s*t\s*\)",
                f"{f.name} 仍定义了单一 typeName，两张表没拆干净")
            self.assertNotRegex(
                body, r"\{\s*friend:\s*\"好友\"\s*,\s*private:",
                f"{f.name} 里 private 与 friend 仍在同一张表（旧的混表写法）")


if __name__ == "__main__":
    unittest.main()