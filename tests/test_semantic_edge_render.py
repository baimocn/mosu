# -*- coding: utf-8 -*-
"""语义边（迭代 12）的前端渲染行为测试。

要验的核心纪律只有一条，但它是这一层最贵的：

    **锚点缺失 → 整条语义边丢弃，绝不画到邻近节点上。**

为什么对语义边比���树线更严：
  树线画错，用户会看出「这个点连错人了」——自己会怀疑；
  语义线画错，用户会以为「这两个分支真的有关系」——不会怀疑，
  直接就信了。**假关系比没线更糟。**

所以这里不只验「有线」，还要验「藏掉节点后线怎么变」：
  - 藏掉一端 → 这条语义边必须消失
  - 剩下别的语义边必须**原封不动**（不能因为少了一条就重画到别处）

跑法：python -m pytest tests/test_semantic_edge_render.py -p no:typeguard -q
"""
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _probe_html() -> str:
    """探针页：固定数据（含语义关联）+ 真实组件。"""
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
__APP_CSS__
</head><body>
<div id="probe" style="width:1100px"></div>
<script src="__VUE__"></script>
<script src="__BT__"></script>
<script>
window.__BT_BRANCHES__ = [
  { id:'b1', goal:'分析我和李小明的聊天往来', status:'complete', receipt:'accepted',
    report:'共 1218 条', cost:12, depth:1, parentId:null, lane:0, laneColor:'#3b82f6',
    steps:[{index:1}], semanticLinks:[
      {from:'b1', to:'b2', kind:'shared-session', label:'都读过「李小明」的聊天', color:'#64748b'}
    ], children:[]},
  { id:'b2', goal:'看看李小明这个月心情如何', status:'complete', receipt:'accepted',
    report:'提到 12 次', cost:8, depth:1, parentId:null, lane:1, laneColor:'#a855f7',
    steps:[{index:1}], semanticLinks:[
      {from:'b1', to:'b2', kind:'shared-session', label:'都读过「李小明」的聊天', color:'#64748b'}
    ], children:[]},
  { id:'b3', goal:'统计王小红的消息数', status:'complete', receipt:'accepted',
    report:'共 1371 条', cost:6, depth:1, parentId:null, lane:2, laneColor:'#06b6d4',
    steps:[{index:1}], semanticLinks:[], children:[]},
];
const app = Vue.createApp({
  components: { 'branch-tree': window.MosuV2.BranchTree },
  setup() {
    const branches = Vue.ref(window.__BT_BRANCHES__);
    window.__BT_ORIG__ = JSON.parse(JSON.stringify(window.__BT_BRANCHES__));
    window.__setBranches = (v) => { branches.value = v; };
    return { branches };
  },
  template: '<div><branch-tree :branches="branches"></branch-tree></div>'
});
app.mount('#probe');
window.__BT_READY__ = true;
</script></body></html>"""


class _Probe:
    def __init__(self, pg):
        self.pg = pg

    def count(self, sel):
        return self.pg.locator(sel).count()

    def sem_paths(self):
        return self.pg.eval_on_selector_all(
            ".v2-bt-sem-line", "els => els.map(e => e.getAttribute('d'))")

    def tree_paths(self):
        return self.pg.eval_on_selector_all(
            ".v2-bt-line", "els => els.map(e => e.getAttribute('d'))")

    def hide(self, branch_id):
        self.pg.eval_on_selector(
            f".v2-bt-node[data-branch-id='{branch_id}']",
            "e => { e.style.display = 'none'; }")

    def show_all(self):
        self.pg.eval_on_selector_all(
            ".v2-bt-node", "els => els.forEach(e => { e.style.display = ''; })")


class TestSemanticEdgeRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("playwright 未安装，跳过前端渲染测试")

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(headless=True)
        cls._page = cls._browser.new_page(viewport={"width": 1440, "height": 900})
        cls.errors = []
        cls._page.on("pageerror", lambda e: cls.errors.append("PAGEERROR: " + str(e)))
        cls._page.on("console", lambda m: cls.errors.append(f"{m.type}: {m.text}")
                     if m.type == "error" else None)

        html = _probe_html()
        html = html.replace("__VUE__",
                            (ROOT / "gui/vendor/vue.global.prod.js").as_uri())
        html = html.replace("__BT__",
                            (ROOT / "gui/assets/v2/views-branch.js").as_uri())
        css = (ROOT / "gui/assets/v2/app.css").read_text(encoding="utf-8")
        theme = (ROOT / "gui/assets/theme.css").read_text(encoding="utf-8")
        html = html.replace("__APP_CSS__", "<style>" + theme + css + "</style>")
        tmp = ROOT / ".cache" / "audit" / "_sem_edge_probe.html"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(html, encoding="utf-8")
        cls._page.goto(tmp.as_uri(), wait_until="networkidle")
        cls._page.wait_for_function("window.__BT_READY__ === true", timeout=15000)
        cls._page.wait_for_timeout(800)
        cls.probe = _Probe(cls._page)

    @classmethod
    def tearDownClass(cls):
        try:
            cls._browser.close()
            cls._pw.stop()
        except Exception:  # noqa: BLE001
            pass

    def setUp(self):
        self.probe.show_all()
        self.probe.pg.evaluate("""() => {
          window.__setBranches(JSON.parse(JSON.stringify(window.__BT_ORIG__)));
        }""")
        self.probe.pg.wait_for_timeout(400)

    # -------------------------------------------------- 基本结构
    def test_semantic_edge_is_rendered(self):
        """b1↔b2 有语义关联 → 必须画出 1 条语义线（虚线图层）。"""
        self.assertEqual(self.probe.count(".v2-bt-sem-line"), 1)

    def test_semantic_and_tree_edges_are_separate_layers(self):
        """语义线与树线**分开计数**——混在一层就分不清关系类型了。

        3 个顶层分支 → 3 条树线（虚拟根连出去），1 条语义线。
        """
        self.assertEqual(self.probe.count(".v2-bt-line"), 3)
        self.assertEqual(self.probe.count(".v2-bt-sem-line"), 1)

    def test_semantic_edge_has_real_path(self):
        for d in self.probe.sem_paths():
            self.assertTrue(d and d.startswith("M") and len(d) > 20,
                            f"语义线的 path 不完整：{d!r}")

    def test_semantic_edge_is_dashed(self):
        """语义线必须是虚线——跟实线树线视觉上分开。"""
        dash = self.probe.pg.eval_on_selector(
            ".v2-bt-sem-line",
            "e => getComputedStyle(e).strokeDasharray")
        self.assertTrue(dash and dash.strip() not in ("", "none"),
                        f"语义线不是虚线：strokeDasharray={dash!r}")

    def test_hit_layer_exists_for_hover(self):
        """必须有独立的宽命中区——虚线本身断续，鼠标落在空隙里就 hover 不到。"""
        self.assertEqual(self.probe.count(".v2-bt-sem-hit"), 1)
        pe = self.probe.pg.eval_on_selector(
            ".v2-bt-sem-hit", "e => getComputedStyle(e).pointerEvents")
        self.assertEqual(pe, "stroke")

    # -------------------------------------------------- 纪律：锚点缺失不乱飞
    def test_hiding_one_end_removes_semantic_edge(self):
        """藏掉语义边的一端 → 这条语义边必须消失。

        如果实现 fallback 到邻近节点，这里会看到线数不变，
        用户就会看到一条**指向别人的假关系线**。
        """
        before = self.probe.count(".v2-bt-sem-line")
        self.assertEqual(before, 1)
        self.probe.hide("b2")
        self.probe.pg.wait_for_timeout(700)
        after = self.probe.count(".v2-bt-sem-line")
        self.assertEqual(after, 0,
                         f"藏掉一端后语义边应消失，实际 {before} → {after}")
        self.probe.show_all()
        self.probe.pg.wait_for_timeout(700)
        self.assertEqual(self.probe.count(".v2-bt-sem-line"), 1)

    def test_hiding_unrelated_node_keeps_semantic_edge(self):
        """藏掉**无关**节点（b3）→ 语义边必须原封不动。

        这条更严：不仅数量对，**坐标也要一样**。
        少画一条线用户看不出来，画错一条线用户会信。
        """
        before = self.probe.sem_paths()
        self.probe.hide("b3")
        self.probe.pg.wait_for_timeout(700)
        after = self.probe.sem_paths()
        self.assertEqual(len(after), len(before),
                         "藏掉无关节点后语义边数量变了")
        self.assertEqual(after, before,
                         f"藏掉无关节点后语义边坐标变了：{before} → {after}")
        self.probe.show_all()
        self.probe.pg.wait_for_timeout(600)

    def test_hiding_all_nodes_removes_all_semantic_edges(self):
        """全藏 → 一条语义线都不能剩。"""
        self.probe.hide("b1")
        self.probe.hide("b2")
        self.probe.hide("b3")
        self.probe.pg.wait_for_timeout(700)
        self.assertEqual(self.probe.count(".v2-bt-sem-line"), 0,
                         "锚点全不可见了还留着线，就是画到了空处")
        self.probe.show_all()
        self.probe.pg.wait_for_timeout(700)

    # -------------------------------------------------- hover 提示
    def test_hover_shows_reason_label(self):
        """hover 语义线 → 显示「为什么有关系」。

        虚线本身不解释自己，用户必须能点/悬停看到理由，
        否则「为什么这两条线有关系」就成了猜谜。

        取点有两个坑：
          ① `getPointAtLength` 返回的是 **SVG 用户坐标**，
             不是视口坐标——必须过一遍 `getScreenCTM()` 才是屏幕坐标；
          ② 不能取路径中点：贝塞尔弧线的中点可能正好落在
             两张节点卡片之间的缝里（那里命中的是别的元素）。
             所以沿路径扫一圈，找**真正命中这条线**的点。
        """
        pt = self.probe.pg.evaluate("""() => {
          const e = document.querySelector('.v2-bt-sem-hit');
          if (!e) return null;
          const m = e.getScreenCTM();
          const total = e.getTotalLength();
          for (let f = 0.5; f >= 0.05; f -= 0.05) {
            const p = e.getPointAtLength(total * f);
            const x = m.a * p.x + m.c * p.y + m.e;
            const y = m.b * p.x + m.d * p.y + m.f;
            if (document.elementFromPoint(x, y) === e) return {x, y};
          }
          return null;
        }""")
        self.assertIsNotNone(pt, "路径上找不到可命中的点（命中区被别的元素挡住了？）")
        self.probe.pg.mouse.move(pt["x"], pt["y"])
        self.probe.pg.wait_for_timeout(500)
        self.assertEqual(self.probe.count(".v2-bt-sem-tip"), 1)
        txt = self.probe.pg.eval_on_selector(
            ".v2-bt-sem-tip", "e => e.textContent.trim()")
        self.assertIn("李小明", txt)

    # -------------------------------------------------- 兼容
    def test_legacy_branch_without_semantic_links(self):
        """老落盘记录没有 semanticLinks 字段 → 不能崩，也不能凭空画线。"""
        self.probe.pg.evaluate(
            "raw => { window.__setBranches(JSON.parse(raw)); }",
            json.dumps([{"id": "x1", "goal": "只有一条", "status": "complete",
                         "receipt": "accepted", "report": "r", "cost": 1,
                         "depth": 1, "parentId": None, "lane": 0,
                         "laneColor": "#3b82f6", "steps": []}],
                       ensure_ascii=False))
        self.probe.pg.wait_for_timeout(700)
        self.assertEqual(self.probe.count(".v2-bt-node"), 1)
        self.assertEqual(self.probe.count(".v2-bt-sem-line"), 0)

    def test_duplicate_links_from_both_ends_render_once(self):
        """同一对分支在后端两端各挂一份 → 前端只能画一条。

        画两条重合的线 = 视觉上像「两层关系」，是假信息。
        """
        self.assertEqual(self.probe.count(".v2-bt-sem-line"), 1)

    def test_no_js_errors(self):
        bad = [e for e in self.errors
               if (e.startswith("PAGEERROR") or e.startswith("error"))
               and "favicon" not in e]
        self.assertEqual(bad, [], f"有 JS 错误：{bad[:5]}")


if __name__ == "__main__":
    unittest.main()
