# -*- coding: utf-8 -*-
"""截断样本的呈现不变量（纯 JS 逻辑，用 node 跑）。

背景：`contact_profile` 有 `CONTACT_MAX_MSGS` 上限。超限时样本只是**最近**
cap 条，会话真实的首条消息并不在样本里。后端为此在
`milestones.firstMessage` 上打了 `sampledOnly: true`。

前端若照常把它渲染成「首条消息」，用户会读成「他们第一次聊天」——
**那是展示假数据**，而且是最难被用户自己发现的那一类：数字看着合理、
日期也能对上，只是它指的根本不是「第一次」。

另一条同类风险：`truncated` 是渲染层信号，泄漏到卡片里会显示成
英文原值 `truncated: true`，属于该被藏起来的内部字段。
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWS_INSIGHT = ROOT / "gui" / "assets" / "views-insight.js"
APP_CSS = ROOT / "gui" / "assets" / "app.css"

NODE = shutil.which("node")
if not NODE:
    for _cand in (r"C:\Users\35955\.workbuddy\binaries\node", r"C:\Program Files\nodejs"):
        _hits = sorted(Path(_cand).glob("**/node.exe"))
        if _hits:
            NODE = str(_hits[-1])
            break


# 在 node 里加载真实的 views-insight.js，取出 JsonView 上的真实方法与
# FIELD_HIDDEN 判定。刻意不复刻逻辑——复刻版会与代码脱节而永远通过。
_SANDBOX = r"""
const fs = require('fs'), vm = require('vm');
const win = {
  Mosu: { fmtNum: String, initial: () => '', highlight: (t) => t,
          avatarColor: () => '#333', toast: {}, api: {}, fmtDur: String },
  MosuViews: { Lightbox: {}, JsonView: {} },
};
win.window = win; win.globalThis = win; win.console = console;
win.setTimeout = setTimeout; win.encodeURIComponent = encodeURIComponent;
win.location = { hash: '' };
vm.createContext(win);
vm.runInContext(fs.readFileSync(process.env.MOSU_VIEWS, 'utf8'), win);
const J = win.MosuViews.JsonView;
const vm2 = (obj) => {
  // visible 是 computed。普通 options 对象里它是 getter 函数；
  // 某些 Vue 构建会包成 {get}。两种都兼容，免得测试反过来依赖实现细节。
  const raw = J.computed.visible;
  const fn = (typeof raw === 'function') ? raw : raw.get;
  const inst = Object.assign({}, J.methods, { data: obj });
  inst.label = J.methods.label;
  inst.isHiddenField = (k) => win.MosuViews.__hidden(k);
  return Object.keys(fn.call(inst));
};
const SAMPLED = { time: '2026-07-07 21:33', who: '对方', content: '（真实文本）', sampledOnly: true };
const REAL   = { time: '2026-05-01 09:00', who: '对方', content: '你好' };
// 实测 14.9 万条的群：纯媒体里程碑的 content 是 null（本来就没正文），
// 首条文本消息的 content 是空串。这两种空值语义不同，不能混为一谈。
const KINDS = {
  firstMessage: { time: 't', who: '对方', content: '', sampledOnly: true },
  firstImage:   { time: 't', who: '对方', content: null, sampledOnly: true },
  firstVoice:   { time: 't', who: '对方', content: null, sampledOnly: true },
  firstVideo:   { time: 't', who: '对方', content: null, sampledOnly: true },
};
console.log(JSON.stringify({
  body: Object.fromEntries(Object.keys(KINDS).map((k) =>
    [k, J.methods.bodyOf(k, KINDS[k])])),
  tip: Object.fromEntries(Object.keys(KINDS).map((k) =>
    [k, J.methods.sampledTip(k)])),
  blankBody: J.methods.bodyOf('firstMessage', { content: '   ' }),
  textBody:  J.methods.bodyOf('firstMessage', { content: '有内容' }),
  unknownBody: J.methods.bodyOf('somethingElse', { content: null }),
  sampledIsSampled: J.methods.isSampledFirst(SAMPLED),
  realIsSampled:    J.methods.isSampledFirst(REAL),
  nullIsSampled:    J.methods.isSampledFirst(null),
  strIsSampled:     J.methods.isSampledFirst('x'),
  emptyObj:         J.methods.isSampledFirst({}),
  sampledFalse:     J.methods.isSampledFirst({ sampledOnly: false }),
  sampledOne:       J.methods.isSampledFirst({ sampledOnly: 1 }),
  keysSampled:      vm2(SAMPLED),
  keysReal:         vm2(REAL),
  keysOverview:     vm2({ totalMessages: 1, truncated: true, spanDays: 3.0 }),
}));
"""


def _node():
    if not NODE or not Path(NODE).exists():
        raise unittest.SkipTest("本机找不到 node，跳过前端渲染断言")
    return NODE


@unittest.skipUnless(NODE, "需要 node 才能验证前端 JS")
class TestSampledOnlyRendering(unittest.TestCase):
    """sampledOnly / truncated 的呈现规则。全部取自真实组件，不复刻。"""

    @classmethod
    def setUpClass(cls):
        import os
        env = dict(os.environ, MOSU_VIEWS=str(VIEWS_INSIGHT))
        r = subprocess.run([_node(), "-e", _SANDBOX],
                           capture_output=True, text=True, timeout=60,
                           cwd=str(ROOT), env=env)
        if r.returncode != 0:
            raise AssertionError(f"加载 views-insight.js 失败：\n{r.stderr[:800]}")
        cls.o = json.loads(r.stdout.strip().splitlines()[-1])

    # ------------------------------------------------- sampledOnly 识别
    def test_sampled_first_message_is_recognised(self):
        """带 sampledOnly 的首条消息必须被识别出来，换成专属渲染。"""
        self.assertTrue(self.o["sampledIsSampled"],
                        "带 sampledOnly 的对象未被识别，会照常渲染成「首条消息」"
                        "——用户会当成真实第一次聊天")

    def test_real_first_message_not_flagged(self):
        """未截断时不能误判：那会让正常数据也显示「样本内最早」。"""
        self.assertFalse(self.o["realIsSampled"],
                         "真实首条消息被误判为样本内最早")

    def test_non_objects_never_flagged(self):
        """null / 字符串 / 空对象都不该被判为 sampledOnly，
        否则 JsonView 的通用分支会被误伤。"""
        for k in ("nullIsSampled", "strIsSampled", "emptyObj"):
            self.assertFalse(self.o[k], f"{k} 被误判为样本内最早")
        self.assertFalse(self.o["sampledFalse"],
                         "sampledOnly:false 被当成 true（应严格 === true）")
        self.assertFalse(self.o["sampledOne"],
                         "sampledOnly:1 被当成 true（应严格 === true，避免后端改成 1/'true' 时静默失效）")

    # ------------------------------------------------------- 字段隐藏
    def test_sampled_only_key_never_renders(self):
        """sampledOnly 是给渲染层的信号，不能作为字段露给用户。"""
        self.assertNotIn("sampledOnly", self.o["keysSampled"],
                         "sampledOnly 泄漏到界面上了")

    def test_real_first_message_keeps_all_fields(self):
        """反过来：正常对象不能被误删字段。"""
        for k in ("time", "who", "content"):
            self.assertIn(k, self.o["keysReal"], f"正常首条消息丢了 {k} 字段")

    def test_truncated_key_never_renders(self):
        """truncated 已由页首脚注说明，卡片里再显示英文原值是噪音。"""
        self.assertNotIn("truncated", self.o["keysOverview"],
                         "truncated 泄漏到卡片里（会显示成英文 true/false）")
        self.assertIn("totalMessages", self.o["keysOverview"],
                      "隐藏 truncated 时误伤了正常字段")

    # ---------------------------------------------------------- 样式
    def test_sampled_first_style_exists(self):
        """警示竖线 + 徽标是「这不是真数据」的可视信号，
        模板引用了 .sampled-first 却没有样式 = 静默失效。

        断言必须锚在**主规则**上：只查 ".sampled-first" 子串的话，
        删掉主规则、留下 `.sampled-first > .badge` 时仍会通过（实测过）。
        """
        css = APP_CSS.read_text(encoding="utf-8")
        m = re.search(r"^\.sampled-first\s*\{([^}]*)\}", css, re.M)
        self.assertIsNotNone(
            m, "app.css 里没有 .sampled-first 主规则，模板引用它等于没样式")
        body = m.group(1)
        self.assertIn("border-left", body,
                      "样本内首条没有警示竖线，用户看不出它与真实首条不同")
        self.assertIn("cursor", body,
                      "没有 cursor:help，tooltip 在鼠标形态下不会被触发")


class TestMilestoneBodyWording(unittest.TestCase):
    """纯媒体里程碑的正文措辞。

    缺陷实测（测试验收师报）：14.9 万条的群里四条里程碑并排显示「（无正文）」，
    观感像功能坏了。但「无正文」本身是**错的措辞**——图片/视频/语音本来就
    没有正文，content 为 null 是正常的，不是内容丢失。
    """

    @classmethod
    def setUpClass(cls):
        import os
        env = dict(os.environ, MOSU_VIEWS=str(VIEWS_INSIGHT))
        r = subprocess.run([_node(), "-e", _SANDBOX],
                           capture_output=True, text=True, timeout=60,
                           cwd=str(ROOT), env=env)
        if r.returncode != 0:
            raise AssertionError(f"加载 views-insight.js 失败：\n{r.stderr[:800]}")
        cls.o = json.loads(r.stdout.strip().splitlines()[-1])

    def test_media_milestones_do_not_say_no_body(self):
        """媒体类里程碑不得显示「无正文」——那是把「本来就没有」说成了「内容丢了」。"""
        for k in ("firstImage", "firstVideo", "firstVoice"):
            self.assertNotIn(
                "无正文", self.o["body"][k],
                f"{k} 显示「{self.o['body'][k]}」，会让用户以为内容丢了")

    def test_media_milestones_name_their_type(self):
        """媒体类应说明是什么消息，让这一行仍有信息量。"""
        self.assertEqual(self.o["body"]["firstImage"], "（图片消息）")
        self.assertEqual(self.o["body"]["firstVideo"], "（视频消息）")
        self.assertEqual(self.o["body"]["firstVoice"], "（语音消息）")

    def test_empty_text_says_no_text_factually(self):
        """首条文本消息 content 为空串：说「该条无文字」而不是「无正文」——
        它确实是条消息，只是不带文字。"""
        self.assertEqual(self.o["body"]["firstMessage"], "（该条无文字）")

    def test_blank_and_real_text_pass_through(self):
        self.assertEqual(self.o["blankBody"], "（该条无文字）",
                         "纯空白应按无文字处理")
        self.assertEqual(self.o["textBody"], "有内容",
                         "有正文时必须原样显示，不能被兜底文案覆盖")

    def test_unknown_key_falls_back_safely(self):
        """后端将来加新的 first* 类型时，不该渲染出 undefined。"""
        self.assertEqual(self.o["unknownBody"], "（该条无文字）")

    def test_wording_is_consistent_across_four_kinds(self):
        """四项措辞不得雷同到看不出区别——那等于没分化。"""
        bodies = [self.o["body"][k] for k in
                  ("firstMessage", "firstImage", "firstVideo", "firstVoice")]
        self.assertEqual(len(set(bodies)), 4,
                         f"四项正文文案有重复：{bodies}")

    def test_tooltip_is_specific_per_kind(self):
        """tooltip 原先统一说「第一次聊天」，对图片/视频不准确。"""
        self.assertIn("第一次聊天", self.o["tip"]["firstMessage"])
        for k, what in (("firstImage", "第一张图片"),
                        ("firstVideo", "第一个视频"),
                        ("firstVoice", "第一条语音")):
            self.assertIn(what, self.o["tip"][k],
                          f"{k} 的 tooltip 未指明是「{what}」")
            self.assertNotIn("第一次聊天", self.o["tip"][k],
                             f"{k} 的 tooltip 不该说「第一次聊天」")


class TestSampledWording(unittest.TestCase):
    """文案与口径的一致性（静态检查，node 不可用时也能跑）。"""

    def setUp(self):
        self.src = VIEWS_INSIGHT.read_text(encoding="utf-8")

    def test_truncated_notice_uses_recent_wording(self):
        """后端已把采样改为 order="desc"（取最近 cap 条），
        提示语必须说「最近」，说「抽样」会让用户不知道覆盖的是哪一段。"""
        self.assertIn("仅基于最近", self.src,
                      "触顶提示未使用「仅基于最近 N 条」——与后端 desc 口径不符")
        self.assertNotIn("本次仅抽样", self.src,
                         "仍残留旧口径的「本次仅抽样」措辞")

    def test_maxmsgs_fallback_preserved(self):
        """maxMsgs 缺失时回退 scannedMessages，别把这条一起删了。"""
        self.assertRegex(self.src, r"m\.maxMsgs\s*\|\|\s*m\.scannedMessages",
                         "maxMsgs 的回退逻辑不见了")

    def test_sample_wording_distinguishes_from_real_first(self):
        """截断样本的首条必须有一个专属措辞，不能与「首条消息」共用。"""
        self.assertIn("样本内最早", self.src,
                      "缺少「样本内最早」措辞，用户无法分辨样本与真实首条")

    def test_deeplink_for_sharing(self):
        """#/profile?session=  深链：没有它时画像链接无法分享，
        也就没法让本轮验收在 GUI 里复现。"""
        self.assertRegex(self.src, r"session=([^&]+)",
                         "画像页不读 hash 里的 session 深链")


if __name__ == "__main__":
    unittest.main()