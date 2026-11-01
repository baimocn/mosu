# -*- coding: utf-8 -*-
"""迭代 4：分支树（泳道 + 语义锚点光线）的静态接线检查。

为什么要静态检查
----------------
分支树的核心是两条**纪律**，它们都不是「跑起来看对不对」能验证的：

1. **颜色按泳道分配一次，全链路继承**
   —— 前端不能自己配色。配色在后端算好下发（branch.laneColor），
   前端只消费。如果前端自己按索引算，两边算法一改就错位，
   而且错位时**不报错**，只是颜色对不上，很难发现。

2. **锚点缺失不乱飞**
   —— 读不到锚点时必须**跳过这条线**，绝不画到邻近节点。
   这条如果写错（画到邻近节点），后果是用户看到一条**假的关系线**，
   而假关系比没线更糟。所以必须用代码结构锁死：
   「读锚点失败 → continue」这个分支必须存在。

这组测试用源码静态检查，任何环境都能跑（不需要跑 GUI 服务）。
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestBranchTreeStaticWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.bt = (ROOT / "gui" / "assets" / "v2" / "views-branch.js") \
            .read_text(encoding="utf-8")
        cls.insight = (ROOT / "gui" / "assets" / "v2" / "views-insight.js") \
            .read_text(encoding="utf-8")
        cls.shell = (ROOT / "gui" / "assets" / "v2" / "shell.js") \
            .read_text(encoding="utf-8")
        cls.index = (ROOT / "gui" / "index.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "gui" / "assets" / "v2" / "app.css") \
            .read_text(encoding="utf-8")
        cls.team = (ROOT / "core" / "agent_team.py").read_text(encoding="utf-8")

    # -------------------------------------------------- 组件被真正接进页面
    def test_branch_tree_script_is_loaded(self):
        """脚本必须在 index.html 里被加载，否则组件根本没注册。"""
        self.assertIn("/assets/v2/views-branch.js", self.index)

    def test_branch_tree_component_is_registered(self):
        """shell.js 必须注册 branch-tree，否则模板里 <branch-tree> 是未知标签。"""
        self.assertIn("branch-tree", self.shell)
        self.assertIn("BranchTree", self.shell)

    def test_insight_registers_branch_tree_locally(self):
        """**洞察视图自己**必须注册 branch-tree。

        Vue 的局部组件注册是**向下**可见的：App 注册了组件，
        App 的子组件（Insight）里**用不了**。
        踩过这个坑：只在 shell.js 注册 → <branch-tree> 成了未知标签，
        Vue **静默忽略不报错**，页面看着正常但分支树永远不出现。
        """
        i = self.insight.index("const Insight = {")
        head = self.insight[i:i + 800]
        self.assertIn("components:", head,
                      "洞察视图没有本地注册组件")
        self.assertIn("branch-tree", head,
                      "洞察视图没注册 branch-tree")

    def test_branch_tree_script_loads_before_insight(self):
        """分支树脚本必须**先于**洞察视图加载。

        洞察视图在定义时就要拿到 global.MosuV2.BranchTree，
        顺序反了 → 拿到 undefined → 注册了个 undefined 组件 → 静默不渲染。
        """
        i_bt = self.index.index("/assets/v2/views-branch.js")
        i_ins = self.index.index("/assets/v2/views-insight.js")
        self.assertLess(i_bt, i_ins,
                        "分支树脚本要在洞察视图之前加载，否则注册到 undefined")

    def test_insight_page_uses_branch_tree(self):
        """洞察页要用这个组件，不能只是造了个文件没人用。"""
        self.assertIn("<branch-tree", self.insight)
        self.assertIn(":branches=", self.insight)

    def test_branch_tree_file_exists(self):
        self.assertTrue((ROOT / "gui" / "assets" / "v2" / "views-branch.js").exists())

    # -------------------------------------------------- 纪律 1：颜色继承
    def test_frontend_does_not_compute_own_colors(self):
        """前端不能自己按索引算颜色——必须用后端下发的 laneColor。

        两边各算一套，一旦调色板改动就会错位，而且**不报错**。
        """
        # 前端允许有 FALLBACK 兜底，但不能按 lane 索引算主色
        m = re.search(r"color:\s*([^,\n]+),", self.bt)
        self.assertIsNotNone(m, "找不到节点颜色的取值表达式")
        expr = m.group(1)
        self.assertIn("laneColor", expr,
                      f"节点颜色应优先用后端下发的 laneColor，实际是：{expr}")

    def test_lane_color_is_inherited_by_css_variable(self):
        """泳道颜色通过 --lc CSS 变量下发，节点继承（不逐个节点写死颜色）。"""
        self.assertIn("--lc", self.css)
        # 节点用的是 var(--lc, ...) 而不是硬编码色值
        self.assertRegex(self.css, r"var\(--lc[^)]*\)")

    def test_backend_computes_lane_color(self):
        """后端必须在 to_dict 里下发 lane 与 laneColor。"""
        self.assertIn("laneColor", self.team)
        self.assertIn("def lane_color", self.team)

    def test_backend_palette_has_no_duplicates(self):
        """调色板不能有重复色——重复色就分不清泳道了。"""
        import re as _re
        m = _re.search(r"LANE_COLORS\s*=\s*\[(.*?)\]", self.team, _re.S)
        self.assertIsNotNone(m, "找不到 LANE_COLORS")
        colors = _re.findall(r"#[0-9a-fA-F]{6}", m.group(1))
        self.assertGreaterEqual(len(colors), 4, "调色板太短")
        self.assertEqual(len(colors), len(set(colors)), "调色板里有重复色")

    def test_children_inherit_parent_lane(self):
        """子分支必须继承父的泳道（Branch 构造里 parent 优先）。"""
        i = self.team.index("class Branch:")
        body = self.team[i:i + 2000]
        self.assertIn("parent.lane", body,
                      "子分支没有继承父的泳道")

    # -------------------------------------------------- 纪律 2：锚点缺失不乱飞
    def test_anchor_missing_skips_the_edge(self):
        """**核心纪律**：读不到锚点就跳过这条线，绝不画到邻近节点。

        Lancer 原话：a clipped anchor cannot be replaced by a nearby node。
        如果这里写成「fallback 到最近的节点」，用户会看到一条**假的关系线**，
        而假关系比没线更糟——用户会以为那两个子代理真有关系。
        """
        i = self.bt.index("function recompute")
        body = self.bt[i:i + 2000]
        self.assertIn("readAnchor", body)
        # 必须有「任一端为 null → continue」的分支
        self.assertRegex(body, r"if\s*\(\s*!a\s*\|\|\s*!b\s*\)\s*continue",
                         "读不到锚点时没有跳过这条线——会画出假关系")
        # 不能有「退而求其次找邻近节点」的逻辑
        self.assertNotIn("nearest", body.lower(), "不能用邻近节点替代缺失的锚点")

    def test_anchor_key_is_semantic_not_index(self):
        """锚点必须用语义标识（分支 id），不能用「第几个元素」。

        用索引的话，列表一变（插入/排序）线就全错位了，而且不报错。
        """
        self.assertIn("data-anchor-key", self.bt)
        self.assertIn("branch:", self.bt)
        self.assertNotRegex(self.bt, r"data-anchor-key=\"\{\{?\s*\d+",
                            "锚点不能用序号")

    def test_anchor_read_checks_visibility(self):
        """读锚点要检查可见性——隐藏元素的坐标是 0,0，画出来是乱线。"""
        i = self.bt.index("function readAnchor")
        body = self.bt[i:i + 1500]
        self.assertIn("getBoundingClientRect", body)
        self.assertTrue(
            "visibility" in body or "display" in body,
            "读锚点没检查元素可见性——隐藏元素会画出 0,0 的乱线")

    def test_curve_is_svg_path(self):
        """连线必须是 SVG path（不是 div 伪元素），否则缩放/换行就错位。"""
        self.assertIn("cubicPath", self.bt)
        self.assertRegex(self.bt, r"M\s.*C\s", "曲线不是三次贝塞尔 path")
        self.assertIn("<svg", self.bt)
        self.assertIn("<path", self.bt)

    def test_lines_layer_does_not_block_interaction(self):
        """连线层要 pointer-events:none，否则会挡住节点上的点击。"""
        i = self.css.index(".v2-bt-lines")
        block = self.css[i:i + 600]
        self.assertIn("pointer-events", block,
                      "连线层没设 pointer-events:none，会挡住节点点击")
        self.assertRegex(block, r"pointer-events:\s*none")

    # -------------------------------------------------- 虚拟根节点
    def test_has_virtual_root_node(self):
        """必须有代表主代理的虚拟根。

        只有一层分支时父子边一条都没有，「树」会退化成几个并列卡片——
        **拓扑关系完全看不出来**。虚拟根让「主代理派了 N 个子代理」
        一眼可见。
        """
        self.assertIn("ROOT_ID", self.bt)
        self.assertIn("branch:root", self.bt,
                      "虚拟根没有 data-anchor-key，连线找不到端点")
        self.assertIn("主代理", self.bt)

    def test_top_level_branches_link_from_root(self):
        """顶层分支必须有一条从根出发的边。

        如果只在有 parentId 时才连边，一层分支 → 连线数 0 → 端到端会判失败。
        """
        i = self.bt.index("function flatten")
        body = self.bt[i:i + 2000]
        self.assertIn("ROOT_ID", body,
                      "flatten 里没有从根出发的边构造")
        # 必须有 else 分支给顶层分支连根
        self.assertRegex(body, r"else\s*\{[^}]*ROOT_ID",
                         "顶层分支没有从虚拟根连出来")

    def test_root_anchor_is_rendered_in_dom(self):
        """根节点必须真的在 DOM 里带锚点标记，否则根连线永远读不到端点。"""
        self.assertRegex(
            self.bt,
            r"data-anchor-key=[\"']branch:root[\"']",
            "根节点模板里没有 branch:root 锚点")
        self.assertIn(".v2-bt-root", self.css, "根节点没有样式")

    def test_root_counts_top_level_branches(self):
        """根上要显示派出了几个子代理（否则只是个装饰）。"""
        self.assertIn("topCount", self.bt)

    # -------------------------------------------------- 持续重算
    def test_recomputes_continuously_not_only_on_mount(self):
        """必须**持续重算**锚点位置，不能只在 mount/resize 时算一次。

        布局会因为很多我们没注册事件的原因改变：文本换行、面板折叠、
        字体加载完成、用户手动隐藏某个节点……
        不持续重算 → 曲线停在旧位置，甚至指向已经消失的节点。

        踩过这个坑：端到端里手动 display:none 藏掉节点，
        连线**一条都没消失**——因为重算从没被触发过。
        而当时的断言「隐藏后连线全部消失」因为 before=0 而**空跑通过**，
        把 bug 盖住了。
        """
        self.assertRegex(self.bt, r"requestAnimationFrame\(\s*loopTick",
                         "没有 rAF 持续重算循环")
        self.assertIn("function loopTick", self.bt)

    def test_recompute_has_dirty_check(self):
        """持续重算必须有脏检查，否则每帧 set 状态 → 重渲染 → 又触发重算，死循环。"""
        i = self.bt.index("function recompute")
        body = self.bt[i:i + 3000]
        self.assertIn("EPS", body, "缺少容差比较（亚像素抖动会每帧触发重渲染）")
        self.assertIn("edges.value", body)

    def test_loop_is_cancelled_on_unmount(self):
        """组件卸载必须停掉 rAF 循环，否则泄漏。"""
        i = self.bt.index("onBeforeUnmount(() =>")
        body = self.bt[i:i + 600]
        self.assertIn("cancelAnimationFrame", body)


if __name__ == "__main__":
    unittest.main()
