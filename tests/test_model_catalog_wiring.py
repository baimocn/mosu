# -*- coding: utf-8 -*-
"""迭代 6：模型选择器的静态接线检查。

为什么需要静态检查
------------------
Vue 的组件注册有一条极安静的失败模式：**局部组件注册是「向下」可见的**。
在上层 shell.js 注册了组件，下层子组件里用不了 → 标签成了未知元素 →
**Vue 静默忽略，不报错**，页面看起来就是"这块没渲染"。

迭代 4 踩过一次（<branch-tree>）。同一个坑在 <model-picker> 上会再踩一次，
所以必须用代码结构锁死。

另外两个必须锁死的点：
  - 脚本加载顺序：视图在定义时就要拿到 global.MosuV2.ModelPicker，
    顺序反了 → 注册 undefined 组件 → 同样静默不渲染。
  - 后端路由：前端调了不存在的路径 → 404 → 整个面板空白。
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestModelPickerWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mp = (ROOT / "gui" / "assets" / "v2" / "views-models.js") \
            .read_text(encoding="utf-8")
        cls.settings = (ROOT / "gui" / "assets" / "v2" / "views-settings.js") \
            .read_text(encoding="utf-8")
        cls.shell = (ROOT / "gui" / "assets" / "v2" / "shell.js") \
            .read_text(encoding="utf-8")
        cls.index = (ROOT / "gui" / "index.html").read_text(encoding="utf-8")
        cls.server = (ROOT / "gui" / "server.py").read_text(encoding="utf-8")
        cls.css = (ROOT / "gui" / "assets" / "v2" / "app.css") \
            .read_text(encoding="utf-8")
        cls.catalog = (ROOT / "core" / "model_catalog.py") \
            .read_text(encoding="utf-8")
        cls.llmcfg = (ROOT / "core" / "llmconfig.py").read_text(encoding="utf-8")

    # ------------------------------------------------------------ 组件注册
    def test_component_is_exported(self):
        """组件必须挂到 global.MosuV2 上，否则谁也拿不到。"""
        self.assertIn("global.MosuV2.ModelPicker = ModelPicker", self.mp)

    def test_settings_registers_component_locally(self):
        """设置页必须**局部注册** model-picker。

        只在 shell.js 注册 → 子组件里 <model-picker> 成了未知标签，
        Vue 静默忽略不报错。迭代 4 的 <branch-tree> 就是这么坏的。
        """
        self.assertRegex(self.settings,
                         r'components\s*:\s*\{[^}]*"model-picker"')
        self.assertIn("<model-picker", self.settings)

    def test_shell_also_registers_component(self):
        """shell.js 也要注册（供其他视图复用）。"""
        self.assertRegex(self.shell,
                         r'"model-picker"\s*:\s*global\.MosuV2\.ModelPicker')

    def test_script_loaded_before_shell(self):
        """加载顺序：views-models.js 必须在 shell.js 之前。

        顺序反了 → shell.js 注册时 global.MosuV2.ModelPicker 还是 undefined
        → 注册 undefined 组件 → 静默不渲染。
        """
        i_models = self.index.find("/assets/v2/views-models.js")
        i_shell = self.index.find("/assets/v2/shell.js")
        self.assertNotEqual(i_models, -1, "index.html 没加载 views-models.js")
        self.assertNotEqual(i_shell, -1, "index.html 没加载 shell.js")
        self.assertLess(i_models, i_shell,
                        "views-models.js 必须在 shell.js 之前加载")

    def test_script_loaded_before_settings(self):
        """views-settings.js 也要在 views-models.js 之后（它要拿组件）。"""
        self.assertLess(self.index.find("/assets/v2/views-models.js"),
                        self.index.find("/assets/v2/views-settings.js"))

    def test_settings_tab_is_reachable_via_hash_route(self):
        """设置页的「大模型」tab 必须登记进 shell.js 的 SUB。

        parseHash 只认 SUB 里有的 id（`SUB.some(s => s.id === m[1])`），
        不登记 → #/settings.model 深链被静默忽略 → tab 停在「账号与密钥」
        → 模型目录根本不渲染。真机端到端抓过这个坑。
        """
        self.assertIn('id: "settings.model"', self.shell)

    def test_all_settings_tabs_registered_in_sub(self):
        """设置页每个 tab 都要能在 SUB 里找到（否则深链和命令面板都够不着）。

        注意映射不是机械的 settings.<tab>：TABS 里第一个是 "account"，
        它对应的路由 id 就是 "settings" 本身（默认 tab）。
        """
        import re as _re
        tabs = _re.findall(r'\{ id: "(\w+)", name: "[^"]+" \}',
                           self.settings[self.settings.find("const TABS"):
                                         self.settings.find("const Settings")])
        sub = set(_re.findall(r'id: "([\w.]+)"', self.shell))
        for t in tabs:
            rid = "settings" if t == "account" else f"settings.{t}"
            self.assertIn(rid, sub,
                          f"设置页 tab「{t}」没登记进 shell.js 的 SUB（期望 {rid}）")

    # ------------------------------------------------------------ 语义锚点
    def test_family_buttons_carry_anchor_keys(self):
        """家族按钮必须带 data-anchor-key="family:<id>"。

        与分支树、光线同一套语义锚点体系；端到端脚本靠它定位。
        """
        self.assertIn("'family:' + (f.id || 'none')", self.mp)

    def test_model_items_expose_state_as_attribute(self):
        """模型项要把三态写进 data-state，端到端要能读。"""
        self.assertIn('data-state="m.state"', self.mp)
        self.assertIn(":data-model-id=\"m.modelId\"", self.mp)
        self.assertIn(":data-endpoint-id=\"m.providerId\"", self.mp)

    # ------------------------------------------------------------ 三态纪律
    def test_non_available_models_are_disabled(self):
        """非可用模型必须 disabled。

        写成「一律可点」的话，用户点了才发现没配 Key——
        这个坑应该在渲染层就被挡住。
        """
        self.assertIn(':disabled="m.state!==\'available\'"', self.mp)

    def test_activate_guard_checks_state(self):
        """activate 里也要再查一次状态（双保险）。"""
        self.assertRegex(self.mp,
                         r'function activate\(m\)\s*\{\s*if \(m\.state !== "available"\)')

    def test_state_has_text_label_not_only_color(self):
        """状态必须带文字标签——只靠颜色，色弱用户和截图都看不出来。"""
        self.assertIn("m.stateLabel", self.mp)

    # ------------------------------------------------------------ 后端路由
    def test_backend_exposes_models_route(self):
        self.assertIn('"/api/models"', self.server)
        for path in ("/api/models/probe", "/api/models/endpoint",
                     "/api/models/activate"):
            self.assertIn(f'"{path}"', self.server, f"缺路由 {path}")

    def test_backend_never_returns_api_key(self):
        """目录接口不能回传 Key 明文。"""
        self.assertIn('"hasKey": bool(', self.catalog)
        # api_models 里不能出现 include_key=True
        seg = self.server[self.server.find("def api_models("):
                          self.server.find("def api_models_probe(")]
        self.assertNotIn("include_key=True", seg)

    def test_backend_functions_import_what_they_use(self):
        """函数内用到的名字必须在本函数里 import。

        踩过的坑：api_models_endpoint_save 里调用了 load_model_catalog()，
        但只 import 了 save_endpoint → NameError → HTTP 500。
        单元测试测不到（那是纯函数路径），真机端到端一调就炸。

        注意：这两个模块都提供同名的核心函数，所以**任一来源**的 import
        都能满足——不能只查 core.llmconfig。
        """
        import re as _re
        for m in _re.finditer(r"def (api_models\w*)\(payload: dict\) -> dict:(.*?)(?=\n    def |\n    def api_config)",
                              self.server, _re.S):
            name, body = m.group(1), m.group(2)
            imported_names = set()
            for mod in ("core.llmconfig", "core.model_catalog"):
                for grp in _re.findall(rf"from {mod} import ([^\n]+)", body):
                    for nm in grp.split(","):
                        imported_names.add(nm.strip())
            used = set(_re.findall(r"\b(load_model_catalog|save_endpoint|"
                                   r"delete_endpoint|set_active|set_enabled_models|"
                                   r"load_llm_config|probe_models)\s*\(", body))
            missing = used - imported_names
            self.assertEqual(missing, set(),
                             f"{name} 用了但没 import：{missing}")

    # ------------------------------------------------------------ 家族规则
    def test_backend_documented_priority_rule(self):
        """「模型名优先于 provider」这条规则必须写在代码注释里。

        它是 Lancer 的核心洞察，写反了不会报错，只会让所有模型显示成 GPT。
        """
        self.assertIn("model_re", self.catalog)
        self.assertRegex(self.catalog,
                         r"for name in \(model_id, label\):")
        # provider 匹配必须排在模型名匹配之后
        i_name = self.catalog.find("for name in (model_id, label):")
        i_prov = self.catalog.find("if provider:")
        self.assertLess(i_name, i_prov, "provider 匹配必须排在模型名匹配之后")

    def test_nine_families_defined(self):
        """9 个家族（Lancer 对齐）。"""
        self.assertEqual(self.catalog.count('"provider_re":'), 9)

    # ------------------------------------------------------------ 样式
    def test_css_defines_component_classes(self):
        for cls in (".v2-mp-fam", ".v2-mp-item", ".v2-mp-fam-badge",
                    ".v2-mp-ep", ".v2-mp-item-state"):
            self.assertIn(cls, self.css, f"缺样式 {cls}")

    def test_css_uses_theme_tokens_not_hardcoded_colors(self):
        """颜色要走设计令牌，别写死——暗色主题会瞎。

        家族徽标的背景色是数据驱动的（后端下发 color），那是允许的例外。
        """
        seg = self.css[self.css.find(".v2-mp {"):]
        seg = seg[:seg.find("/* --------------------------------------------------------------- 响应式")]
        self.assertNotRegex(seg, r"#[0-9a-fA-F]{6}",
                             "组件样式里出现了写死的颜色")
        # 徽标背景色来自数据，用 var() 兜底
        self.assertIn("background: var(", self.css[self.css.find(".v2-mp-fam-badge"):])

    # ------------------------------------------------------------ 配置兼容
    def test_legacy_flat_config_still_supported(self):
        """旧扁平配置必须继续被读（向后兼容是硬要求）。"""
        self.assertIn("def _legacy_to_endpoint", self.llmcfg)
        self.assertIn('file_cfg.get("baseUrl")', self.llmcfg)

    def test_env_priority_preserved(self):
        """环境变量优先级不能被多端点破坏。"""
        seg = self.llmcfg[self.llmcfg.find("def load_llm_config"):
                          self.llmcfg.find("def _active_flat")]
        self.assertIn('os.environ.get(_ENV_KEYS["baseUrl"])', seg)
        self.assertIn('os.environ.get(_ENV_KEYS["model"])', seg)
        self.assertLess(seg.find('os.environ.get(_ENV_KEYS["baseUrl"])'),
                        seg.find("active.get("))

    def test_activate_writes_back_flat_fields(self):
        """切换模型必须回写扁平字段，否则下游读的还是旧模型。"""
        seg = self.llmcfg[self.llmcfg.find("def set_active"):
                          self.llmcfg.find("def set_enabled_models")]
        self.assertIn('cfg["baseUrl"] = target["baseUrl"]', seg)
        self.assertIn('cfg["model"] = mid', seg)

    def test_no_duplicate_const_in_setup_scope(self):
        """同一个 setup 作用域里不能有两个同名 const。

        views-models.js 里踩过一次：currentFamily 定义了两次
        （一个是"选中的家族"、一个是"当前模型的家族"）→ SyntaxError
        → 整个组件挂掉。这是 JS 语法错误，node --check 能抓到。
        """
        seg = self.mp[self.mp.find("setup(props)"):]
        consts = re.findall(r"const (\w+) = (?:ref|computed)\(", seg)
        dupes = {c for c in consts if consts.count(c) > 1}
        self.assertEqual(dupes, set(), f"setup 里重复定义：{dupes}")


if __name__ == "__main__":
    unittest.main()
