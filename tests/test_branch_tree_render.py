# -*- coding: utf-8 -*-
"""分支树前端渲染行为测试（离线，不依赖 GUI 服务在跑）。

为什么要有这组测试
------------------
分支树的核心是**两条纪律**，它们都是「跑起来看对不对」能验证的，
但**静态检查验证不了**：

1. **锚点缺失不乱飞**
   —— 藏掉某个节点，只有连到它的线该消失，其他线必须原封不动。
   如果实现写成「fallback 到邻近节点」，用户会看到一条**假的关系线**，
   而假关系比没线更糟。

2. **持续重算**
   —— 布局变了（文本换行、节点被藏、数据换了），
   曲线端点必须跟上。少了这条，曲线会停在旧位置甚至指向已消失的节点。

这两条都**不会抛异常、不会打 console error**，静态源码检查只能确认
「代码里写了 continue」，不能确认「运行时真的跳过了」。

做法
----
起一个独立的探针页（file:// 加载，不经过 GUI 服务），
用**固定**的分支数据挂载真实的 BranchTree 组件，
然后模拟用户操作并断言 DOM 结果。

数据固定 → 不跑模型 → 整个测试 5 秒内跑完 → 可以当回归测试每次都跑。

跑法：python -m pytest tests/test_branch_tree_render.py -p no:typeguard -q
"""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE = ROOT / "tests" / "fixtures" / "branch_tree_probe.html"


def _probe_html() -> str:
    """探针页：固定数据 + 真实组件 + 可控的数据替换入口。"""
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
    steps:[{index:1}], children:[
      { id:'b1c1', goal:'细分时间段', status:'complete', receipt:'accepted',
        report:'按月分布', cost:4, depth:2, parentId:'b1', lane:0, laneColor:'#3b82f6',
        steps:[{index:1}] }
    ]},
  { id:'b2', goal:'分析我和赵小刚的聊天往来', status:'complete', receipt:'accepted',
    report:'共 1197 条', cost:10, depth:1, parentId:null, lane:1, laneColor:'#a855f7',
    steps:[{index:1}] },
  { id:'b3', goal:'分析我和王小红的聊天往来', status:'failed', receipt:'reported',
    error:'超时', cost:45, depth:1, parentId:null, lane:2, laneColor:'#06b6d4',
    steps:[{index:1}] },
];
const app = Vue.createApp({
  components: { 'branch-tree': window.MosuV2.BranchTree },
  setup() {
    const branches = Vue.ref(window.__BT_BRANCHES__);
    // 原始数据留一份，供测试重置（换数据会污染后续用例）
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
    """探针页的封装：起页面 + 几个常用查询。"""

    def __init__(self, pg):
        self.pg = pg

    def count(self, sel):
        return self.pg.locator(sel).count()

    def paths(self):
        return self.pg.eval_on_selector_all(
            ".v2-bt-line", "els => els.map(e => e.getAttribute('d'))")

    def node_borders(self):
        raw = self.pg.eval_on_selector_all(
            ".v2-bt-node",
            "els => els.map(e => [e.getAttribute('data-branch-id'),"
            " getComputedStyle(e).borderLeftColor])")
        return dict(raw)

    def hide(self, branch_id):
        self.pg.eval_on_selector(
            f".v2-bt-node[data-branch-id='{branch_id}']",
            "e => { e.style.display = 'none'; }")

    def show_all(self):
        self.pg.eval_on_selector_all(
            ".v2-bt-node", "els => els.forEach(e => { e.style.display = ''; })")

    def stretch_goals(self):
        """把节点文案拉长 → 节点变高 → 锚点坐标必须变。"""
        self.pg.eval_on_selector_all(
            ".v2-bt-node-goal",
            "els => els.forEach(e => { e.textContent = '很长的目标描述'.repeat(20); })")


class TestBranchTreeRender(unittest.TestCase):
    """分支树渲染行为（真浏览器 + 真实组件 + 固定数据）。"""

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
        # theme.css 的设计令牌单独内联（探针页不引 theme.css 免得依赖 http 服务）
        theme = (ROOT / "gui/assets/theme.css").read_text(encoding="utf-8")
        html = html.replace("__APP_CSS__", "<style>" + theme + css + "</style>")
        # 必须落成真文件再用 file:// 打开：
        # set_content 用的是 about:blank 源，加载 file:// 脚本会被浏览器拦掉。
        tmp = ROOT / ".cache" / "audit" / "_bt_render_probe.html"
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

    # -------------------------------------------------- 基础结构
    def setUp(self):
        """每个用例前重置探针状态。

        不重置的话，「换一组数据」那个用例会污染后面的用例
        （数据只剩 1 条分支，泳道断言就挂了）。
        单个跑都过、一起跑就挂——这种污染最难查，必须显式重置。
        """
        self.probe.show_all()
        self.probe.pg.evaluate("""() => {
          window.__setBranches(JSON.parse(JSON.stringify(window.__BT_ORIG__)));
        }""")
        self.probe.pg.wait_for_timeout(400)

    def test_virtual_root_is_rendered(self):
        """虚拟根（主代理）必须在 DOM 里，且带语义锚点。"""
        self.assertEqual(self.probe.count(".v2-bt-root"), 1)
        key = self.probe.pg.eval_on_selector(
            ".v2-bt-root", "e => e.getAttribute('data-anchor-key')")
        self.assertEqual(key, "branch:root")

    def test_lanes_and_nodes_rendered(self):
        """3 个顶层分支 → 3 条泳道；含 1 个孙节点 → 共 4 个节点。"""
        self.assertEqual(self.probe.count(".v2-bt-lane"), 3)
        self.assertEqual(self.probe.count(".v2-bt-node"), 4)

    def test_every_edge_has_a_real_path(self):
        """每条边都必须有实际路径（不是空 d）。"""
        ds = self.probe.paths()
        self.assertEqual(len(ds), 4, f"应有 4 条边（3 根连 + 1 孙连），实际 {len(ds)}")
        for d in ds:
            self.assertTrue(d and d.startswith("M") and len(d) > 20,
                            f"边的 path 不完整：{d!r}")

    # -------------------------------------------------- 纪律 1：颜色继承
    def test_lane_colors_differ(self):
        """不同泳道颜色必须不同，否则分不清哪条是哪个子代理。"""
        lc = self.probe.pg.eval_on_selector_all(
            ".v2-bt-lane", "els => els.map(e => e.style.getPropertyValue('--lc'))")
        self.assertEqual(len(lc), 3)
        self.assertEqual(len(set(lc)), 3, f"泳道撞色了：{lc}")

    def test_child_inherits_parent_lane_color(self):
        """孙节点必须继承父的泳道色（整条链一个颜色）。"""
        m = self.probe.node_borders()
        self.assertIn("b1c1", m)
        self.assertEqual(m.get("b1c1"), m.get("b1"),
                         f"孙节点没继承父泳道色：父={m.get('b1')} 孙={m.get('b1c1')}")

    # -------------------------------------------------- 纪律 2：锚点缺失不乱飞
    def test_hiding_one_node_removes_only_its_edge(self):
        """藏掉一个节点 → 只有连到它的线该消失，其他线原封不动。

        这是「假关系比没线更糟」的落点：如果实现 fallback 到邻近节点，
        这里会看到线数不变（甚至变多）。
        """
        before = self.probe.count(".v2-bt-line")
        self.assertEqual(before, 4)
        self.probe.hide("b2")
        self.probe.pg.wait_for_timeout(700)
        after = self.probe.count(".v2-bt-line")
        self.assertEqual(after, before - 1,
                         f"藏一个节点应少一条线，实际 {before} → {after}")
        # 剩下的线必须还是有效的路径（没有指向空处）
        for d in self.probe.paths():
            self.assertTrue(d and d.startswith("M"), f"剩余边的 path 坏了：{d!r}")
        self.probe.show_all()
        self.probe.pg.wait_for_timeout(700)
        self.assertEqual(self.probe.count(".v2-bt-line"), before)

    def test_hiding_all_nodes_removes_all_edges(self):
        """全藏 → 一条线都不能剩（没有线可以落在空处）。"""
        self.probe.hide("b1")
        self.probe.hide("b2")
        self.probe.hide("b3")
        self.probe.pg.wait_for_timeout(700)
        self.assertEqual(self.probe.count(".v2-bt-line"), 0,
                         "全部锚点都不可见了，还留着线就是画到了空处")
        self.probe.show_all()
        self.probe.pg.wait_for_timeout(700)

    # -------------------------------------------------- 纪律 3：持续重算
    def test_curve_follows_layout_change(self):
        """布局变了 → 曲线端点必须跟着动。

        少了持续重算，曲线会停在旧位置——用户看到的是「线连错地方」。
        """
        before = self.probe.paths()
        self.probe.stretch_goals()
        self.probe.pg.wait_for_timeout(900)
        after = self.probe.paths()
        self.assertNotEqual(before, after, "布局变了但曲线坐标没更新")
        self.probe.show_all()
        self.probe.pg.wait_for_timeout(500)

    def test_curve_follows_data_change(self):
        """换成单分支数据 → 泳道/节点/线都要跟着变。"""
        self.probe.pg.evaluate("""() => {
          window.__setBranches([
            {id:'x1', goal:'只有一条', status:'complete', receipt:'accepted',
             report:'r', cost:1, depth:1, parentId:null, lane:0,
             laneColor:'#3b82f6', steps:[]}
          ]);
        }""")
        self.probe.pg.wait_for_timeout(900)
        self.assertEqual(self.probe.count(".v2-bt-lane"), 1)
        self.assertEqual(self.probe.count(".v2-bt-node"), 1)
        self.assertEqual(self.probe.count(".v2-bt-line"), 1)
        for d in self.probe.paths():
            self.assertTrue(d and d.startswith("M"), f"单分支时路径坏了：{d!r}")

    def test_no_js_errors(self):
        """整个过程不能有 JS 错误。

        例外：file:// 页面一定会去要 /favicon.ico，那是资源加载失败、
        不是代码错误，跟被测行为无关。
        """
        bad = [e for e in self.errors
               if (e.startswith("PAGEERROR") or e.startswith("error"))
               and "favicon" not in e]
        self.assertEqual(bad, [], f"有 JS 错误：{bad[:5]}")

    def test_virtual_root_is_horizontally_centered(self):
        """虚拟根必须**水平居中**——它是「主代理」，偏在边上就不像根了。

        踩过的坑：`display:inline-flex` + `margin:0 auto` **不会**居中
        （inline-level 元素忽略 auto 外边距），根节点偏在左边。
        这种问题静态检查看不出来，只能真渲染量坐标。
        """
        box = self.probe.pg.eval_on_selector(
            ".v2-bt-root",
            "e => { const r = e.getBoundingClientRect();"
            " const p = e.parentElement.getBoundingClientRect();"
            " return { cx: r.left + r.width / 2, px: p.left + p.width / 2,"
            "          pw: p.width, rw: r.width }; }")
        off = abs(box["cx"] - box["px"])
        self.assertLess(off, 2.0,
                        f"虚拟根没居中：中心偏移 {off:.1f}px（父容器宽 {box['pw']:.0f}）")


if __name__ == "__main__":
    unittest.main()
