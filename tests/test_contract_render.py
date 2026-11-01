# -*- coding: utf-8 -*-
"""阶段契约前端渲染行为测试（离线，不依赖 GUI 服务在跑）。

为什么要有这组测试
------------------
阶段契约的三条纪律都**不会抛异常**，静态源码检查只能确认
「代码里写了 v-if」，不能确认「运行时真的按状态渲染」：

1. **三态要真的区分开**
   —— 通过 / 未通过 / 未涉及，三种情况在 DOM 上必须不同。
   如果实现写成「一律显示绿色」，用户看到「数字无出处」也是绿的，
   等于这个功能没做。

2. **未通过时要真的挂警告**
   —— okToProceed=false 时必须出现警告块，
   并且警告文案来自后端的 hint（前端不能自己编文案）。

3. **违反的禁止项要真的列出来**
   —— forbidden 里 violated=true 的才显示。

做法
----
起独立探针页（file:// 加载），挂载**真实**的洞察视图组件，
mock 掉 API（不真连后端），灌一份固定的契约结果，然后断言 DOM。

跑法：python -m pytest tests/test_contract_render.py -p no:typeguard -q
"""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


# 一份「全部通过」的契约结果
CONTRACT_OK = {
    "stages": [
        {"id": "scope", "label": "范围认定", "actor": "local",
         "goal": "确认这次问的是谁、哪段时间", "required": False,
         "status": {"code": "OK", "label": "通过", "okToProceed": True,
                    "hint": "", "detail": ""},
         "summary": "stage=scope status=OK calls=1"},
        {"id": "probe", "label": "数据探测", "actor": "local",
         "goal": "取到统计与检索结果", "required": True,
         "status": {"code": "OK", "label": "通过", "okToProceed": True,
                    "hint": "", "detail": ""},
         "summary": "stage=probe status=OK calls=2 evidence=5"},
        {"id": "read", "label": "原文取证", "actor": "local",
         "goal": "读到消息原文，供引语落地", "required": False,
         "status": {"code": "OK", "label": "通过", "okToProceed": True,
                    "hint": "", "detail": ""},
         "summary": "stage=read status=OK calls=1 quotes=3"},
        {"id": "verify", "label": "答案核验", "actor": "local",
         "goal": "核对答案里的数字有没有出处", "required": True,
         "status": {"code": "OK", "label": "通过", "okToProceed": True,
                    "hint": "", "detail": ""},
         "summary": "stage=verify status=OK numbers=4 unsourced=0"},
    ],
    "gates": [],
    "forbidden": [
        {"id": "no_number_without_source", "text": "答案里的数字必须有工具返回做出处",
         "why": "w", "violated": False, "detail": ""},
        {"id": "no_stat_as_quote", "text": "统计口径不能当某人说过的话",
         "why": "w", "violated": False, "detail": ""},
    ],
    "checks": [],
    "okToProceed": True,
    "summary": "scope:OK | probe:OK | read:OK | verify:OK",
}

# 一份「有环节未通过」的契约结果
CONTRACT_BAD = {
    "stages": [
        {"id": "scope", "label": "范围认定", "actor": "local",
         "goal": "g", "required": False,
         "status": {"code": "SKIPPED", "label": "未涉及", "okToProceed": True,
                    "hint": "", "detail": ""},
         "summary": "stage=scope status=SKIPPED calls=0"},
        {"id": "probe", "label": "数据探测", "actor": "local",
         "goal": "g", "required": True,
         "status": {"code": "EMPTY", "label": "查无数据", "okToProceed": False,
                    "hint": "这一阶段执行了但没取到任何东西，结论可能只覆盖了部分数据。",
                    "detail": ""},
         "summary": "stage=probe status=EMPTY calls=1 evidence=0"},
        {"id": "read", "label": "原文取证", "actor": "local",
         "goal": "g", "required": False,
         "status": {"code": "NO_QUOTE", "label": "缺原话支撑", "okToProceed": False,
                    "hint": "结论引用了具体话语，但没有对应的消息原文可核对。",
                    "detail": ""},
         "summary": "stage=read status=NO_QUOTE calls=1 quotes=0"},
        {"id": "verify", "label": "答案核验", "actor": "local",
         "goal": "g", "required": True,
         "status": {"code": "UNSOURCED_NUMBER", "label": "数字无出处",
                    "okToProceed": False,
                    "hint": "答案里有数字在工具返回里找不到出处，需人工复核。",
                    "detail": "9999"},
         "summary": "stage=verify status=UNSOURCED_NUMBER numbers=4 unsourced=1"},
    ],
    "gates": [],
    "forbidden": [
        {"id": "no_number_without_source", "text": "答案里的数字必须有工具返回做出处",
         "why": "w", "violated": True, "detail": "9999"},
        {"id": "no_stat_as_quote", "text": "统计口径不能当某人说过的话",
         "why": "w", "violated": False, "detail": ""},
    ],
    "checks": [],
    "okToProceed": False,
    "summary": "scope:SKIPPED | probe:EMPTY | read:NO_QUOTE | verify:UNSOURCED_NUMBER",
}


def _probe_html() -> str:
    """探针页：mock 的 API + 真实洞察视图组件。"""
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
__APP_CSS__
</head><body>
<div id="probe" style="width:1000px;padding:20px"></div>
<script src="__VUE__"></script>
<script>
window.Mosu = {
  api: {
    get: async (p, q) => {
      if (p === '/api/insight/presets') return { presets: [] };
      if (p === '/api/stats/global') return { totalMessages: 100, totalSessions: 5,
                                            totalContacts: 5 };
      if (p === '/api/stats/contacts') return { contacts: [] };
      if (p === '/api/insight/raw') return {};
      if (p === '/api/runs') return { runs: [] };
      if (p === '/api/task') {
        // 轮询保底路径：模拟一个已完成的洞察任务，
        // 结果里带阶段契约 —— 走的是**前端真实代码路径**
        return {
          id: q && q.id, status: 'done', cost: 1,
          live: { steps: [] },
          result: {
            answer: '测试答案', steps: [], evidence: [], branches: [],
            contract: window.__CONTRACT__ || {}, model: 'test-model',
          },
        };
      }
      return {};
    },
    post: async (p) => {
      if (p === '/api/insight/ask') return { taskId: 't1', status: 'running' };
      return { ok: true };
    },
  },
  fmtNum: (n) => String(n),
  toast: { ok: () => {}, err: () => {} },
};
window.__CONTRACT__ = null;
/* EventSource 在 file:// 下必然被 CORS 拦（浏览器只允许 http/https 跨域），
   报错是**探针页的环境限制，不是组件 bug**——
   组件本来就设计成 SSE 挂了自动降级轮询，这条降级路径正是我们要测的。
   所以把 EventSource 换成桩：只触发 error 事件，强制走轮询保底。 */
window.EventSource = class {
  constructor(url) {
    this.url = url; this._h = {};
    setTimeout(() => {
      const fns = this._h.error || [];
      fns.forEach(fn => fn({}));
    }, 0);
  }
  addEventListener(k, fn) { (this._h[k] = this._h[k] || []).push(fn); }
  close() {}
};
</script>
<!-- views-insight.js 顶部就引用 global.MosuV2.BranchTree，
     所以必须先加载 views-branch.js——否则注册子组件时抛错，
     整个视图挂不上（表现为 __INSIGHT_READY__ 一直不置位）。 -->
<script src="__BRANCH__"></script>
<script src="__INSIGHT__"></script>
<script>
/* branch-tree 是洞察视图的子组件——它也必须挂上，
   否则 Vue 会报未知组件（虽然不影响阶段带，但会污染 console 错误断言） */
const app = Vue.createApp({
  components: {
    'insight': window.MosuV2.Insight,
    'branch-tree': window.MosuV2.BranchTree,
  },
  template: '<div><insight></insight></div>'
});
        app.mount('#probe');
/* 把组件实例挂到 window 上：Vue 3 的 app._instance 在某些构建下为 null，
   直接从 DOM 节点的 __vueParentComponent 拿不到 setupState，
   所以在模板里显式把 ref 暴露出来最稳。 */
window.__INSIGHT_READY__ = true;
</script></body></html>"""


class _Probe:
    def __init__(self, pg):
        self.pg = pg

    def count(self, sel):
        return self.pg.locator(sel).count()

    def stage_rows(self):
        return self.pg.eval_on_selector_all(
            ".v2-ia-cstage",
            "els => els.map(e => [e.getAttribute('data-stage-id'),"
            " e.getAttribute('data-status-code'),"
            " e.getAttribute('data-ok-to-proceed'),"
            " e.className])")

    def warn_text(self):
        el = self.pg.locator(".v2-ia-contract-warn")
        if el.count() == 0:
            return ""
        return el.first.inner_text()

    def forbid_text(self):
        el = self.pg.locator(".v2-ia-contract-forbid")
        if el.count() == 0:
            return ""
        return el.first.inner_text()

    def head_count_text(self):
        el = self.pg.locator(".v2-ia-contract .v2-ia-steps-count")
        if el.count() == 0:
            return ""
        return el.first.inner_text()


class TestContractRender(unittest.TestCase):
    """阶段契约前端渲染行为（真浏览器 + 真实组件 + 固定数据）。"""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("playwright 未安装，跳过前端渲染测试")

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(headless=True)
        cls._page = cls._browser.new_page(viewport={"width": 1440, "height": 1200})
        cls.errors = []
        cls._page.on("pageerror", lambda e: cls.errors.append("PAGEERROR: " + str(e)))
        cls._page.on("console", lambda m: cls.errors.append(f"{m.type}: {m.text}")
                     if m.type == "error" else None)

        html = _probe_html()
        html = html.replace("__VUE__",
                            (ROOT / "gui/vendor/vue.global.prod.js").as_uri())
        html = html.replace("__INSIGHT__",
                            (ROOT / "gui/assets/v2/views-insight.js").as_uri())
        html = html.replace("__BRANCH__",
                            (ROOT / "gui/assets/v2/views-branch.js").as_uri())
        css = (ROOT / "gui/assets/v2/app.css").read_text(encoding="utf-8")
        theme = (ROOT / "gui/assets/theme.css").read_text(encoding="utf-8")
        html = html.replace("__APP_CSS__", theme + "\n" + css)

        tmp = ROOT / ".cache" / "audit" / "_contract_render_probe.html"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(html, encoding="utf-8")
        cls._page.goto(tmp.as_uri(), wait_until="networkidle")
        cls._page.wait_for_function("window.__INSIGHT_READY__ === true")

    @classmethod
    def tearDownClass(cls):
        try:
            cls._browser.close()
            cls._pw.stop()
        except Exception:
            pass

    def _inject(self, contract):
        """用**真实问答流程**驱动一轮：填问题 → 提交 → 走轮询保底路径。

        为什么不用「直接改组件内部 turns」：
        那样只测了模板渲染，没测「后端返回的 contract 有没有真的进到 turn 里」。
        走真实流程能同时验证这两段——而后者正是最容易漏的地方
        （前端模板写对了，但后端字段没传过来，界面上一片空白）。
        """
        self._page.evaluate("(c) => { window.__CONTRACT__ = c; }", contract)
        self._page.fill(".v2-ia-input", "测试问题")
        # 提交按钮：模板里的 .v2-ia-go
        self._page.click(".v2-ia-go")
        # 等这一轮跑完（pending 变 false → 契约块渲染出来）
        self._page.wait_for_function(
            "document.querySelector('.v2-ia-contract') !== null "
            "|| document.querySelector('.v2-ia-turn .v2-note.danger') !== null",
            timeout=15000)

    def setUp(self):
        self.p = _Probe(self._page)
        self._page.reload(wait_until="networkidle")
        self._page.wait_for_function("window.__INSIGHT_READY__ === true")

    def test_no_console_or_page_errors(self):
        """组件不能有 JS 错误——Vue 静默失败最常见。"""
        self.assertEqual(self.errors, [], f"前端报错：{self.errors}")

    def test_contract_block_not_rendered_without_data(self):
        """没有契约数据时不渲染这个块（不能留个空壳）。"""
        self.assertEqual(self.p.count(".v2-ia-contract"), 0)

    def test_all_ok_renders_four_stages(self):
        """四个阶段都要渲染出来。"""
        self._inject(CONTRACT_OK)
        self.assertEqual(self.p.count(".v2-ia-contract"), 1)
        self.assertEqual(self.p.count(".v2-ia-cstage"), 4)

    def test_stage_rows_carry_status_attributes(self):
        """每个阶段行要带状态码与 okToProceed（端到端靠它定位）。"""
        self._inject(CONTRACT_OK)
        rows = self.p.stage_rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual([r[0] for r in rows],
                         ["scope", "probe", "read", "verify"])
        for r in rows:
            self.assertTrue(r[1], "缺 status-code")
            self.assertIn(r[2], ("true", "false"))

    def test_all_ok_has_no_warning_block(self):
        """全部通过时不显示警告块。"""
        self._inject(CONTRACT_OK)
        self.assertEqual(self.p.count(".v2-ia-contract-warn"), 0)
        self.assertIn("全部通过", self.p.head_count_text())

    def test_blocked_shows_warning_block(self):
        """有环节未通过 → 出现警告块。"""
        self._inject(CONTRACT_BAD)
        self.assertEqual(self.p.count(".v2-ia-contract-warn"), 1)
        self.assertIn("有环节未通过", self.p.head_count_text())

    def test_warning_uses_backend_hint_text(self):
        """警告文案必须用后端给的 hint（前端不能自己编）。"""
        self._inject(CONTRACT_BAD)
        txt = self.p.warn_text()
        self.assertIn("这一阶段执行了但没取到任何东西", txt)
        self.assertIn("需人工复核", txt)

    def test_skipped_stage_is_not_marked_bad(self):
        """SKIPPED 不算「未通过」——不该被标黄。"""
        self._inject(CONTRACT_BAD)
        rows = {r[0]: r for r in self.p.stage_rows()}
        self.assertEqual(rows["scope"][1], "SKIPPED")
        self.assertEqual(rows["scope"][2], "true")
        self.assertNotIn("bad", rows["scope"][3])

    def test_failed_stage_is_marked_bad(self):
        """未通过的阶段要标 bad（视觉上区分得开）。"""
        self._inject(CONTRACT_BAD)
        rows = {r[0]: r for r in self.p.stage_rows()}
        self.assertIn("bad", rows["probe"][3])
        self.assertIn("bad", rows["verify"][3])

    def test_violated_forbidden_is_listed(self):
        """违反的禁止项要列出来。"""
        self._inject(CONTRACT_BAD)
        self.assertEqual(self.p.count(".v2-ia-contract-forbid"), 1)
        txt = self.p.forbid_text()
        self.assertIn("答案里的数字必须有工具返回做出处", txt)
        self.assertIn("9999", txt)

    def test_non_violated_forbidden_not_listed(self):
        """没违反的禁止项不显示（不制造噪音）。"""
        self._inject(CONTRACT_OK)
        self.assertEqual(self.p.count(".v2-ia-contract-forbid"), 0)

    def test_mechanical_summary_is_visible(self):
        """机械摘要要显示出来（用户能自己核对计数）。"""
        self._inject(CONTRACT_OK)
        txt = self._page.locator(".v2-ia-cstage-sum").first.inner_text()
        self.assertIn("stage=scope", txt)


if __name__ == "__main__":
    unittest.main()
