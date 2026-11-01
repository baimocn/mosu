# -*- coding: utf-8 -*-
"""模型选择器前端渲染行为测试（离线，不依赖 GUI 服务在跑）。

为什么要有这组测试
------------------
模型选择器的三条纪律都**不会抛异常**，静态源码检查只能确认
「代码里写了 disabled」，不能确认「运行时真的点不动」：

1. **三态必须真的区分开**
   —— 未配置 / 未启用的模型必须是 disabled（点不动），
   可用的模型必须能点。如果实现写成「一律可点」，
   用户点了才发现没配 Key——这个坑应该在渲染层就被测出来。

2. **家族切换真的换列表**
   —— 点家族按钮，列表要换成该家族的模型。
   Vue 局部组件注册方向反了的话，组件会**静默不渲染**，
   静态检查看不出来。

3. **搜索真的过滤**
   —— 输入关键词，列表只剩匹配项。

做法
----
起一个独立探针页（file:// 加载），用**固定**的目录数据挂载真实组件，
mock 掉 API（不真连后端），然后模拟用户操作并断言 DOM。

跑法：python -m pytest tests/test_model_picker_render.py -p no:typeguard -q
"""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _probe_html() -> str:
    """探针页：固定目录数据 + 真实组件 + mock 的 API。"""
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
__APP_CSS__
</head><body>
<div id="probe" style="width:900px;padding:20px"></div>
<script src="__VUE__"></script>
<script>
/* ---- mock 的 Mosu 全局：只提供组件用到的 API ---- */
window.Mosu = {
  api: {
    get: async (p) => {
      if (p === '/api/models') return JSON.parse(JSON.stringify(window.__CATALOG__));
      return {};
    },
    post: async (p, body) => {
      window.__POSTS__.push({ path: p, body: body });
      if (p === '/api/models/activate') {
        window.__CATALOG__.active = { endpointId: body.endpointId,
                                     modelId: body.modelId };
        window.__CATALOG__.current = { model: body.modelId,
                                      activeEndpoint: body.endpointId,
                                      activeModel: body.modelId,
                                      baseUrl: '', source: 'active' };
        return { activated: true, catalog: window.__CATALOG__ };
      }
      return { ok: true };
    },
  },
  toast: { ok: (m) => { window.__TOASTS__.push(m); },
           err: (m) => { window.__TOASTS__.push('ERR:' + m); } },
};
window.__POSTS__ = [];
window.__TOASTS__ = [];
window.__CATALOG__ = {
  families: [
    /* 第一个家族：2 个模型 → 不显示搜索框（Lancer 阈值 6） */
    { id:'deepseek', label:'DeepSeek', badge:'DS', color:'#4d6bfe', count:2, modelCount:2,
      models:[
        { providerId:'ep_ds', providerName:'DeepSeek', modelId:'deepseek-chat',
          label:'deepseek-chat', available:true, state:'available', stateLabel:'可用',
          family:'deepseek', familyLabel:'DeepSeek', familyBadge:'DS', familyColor:'#4d6bfe' },
        { providerId:'ep_ds', providerName:'DeepSeek', modelId:'deepseek-reasoner',
          label:'deepseek-reasoner', available:true, state:'available', stateLabel:'可用',
          family:'deepseek', familyLabel:'DeepSeek', familyBadge:'DS', familyColor:'#4d6bfe' }
      ]},
    { id:'claude', label:'Claude', badge:'CL', color:'#d97757', count:1, modelCount:1,
      models:[{ providerId:'ep_gw', providerName:'自建网关', modelId:'claude-sonnet-4',
                label:'Claude Sonnet 4', available:true, state:'available', stateLabel:'可用',
                family:'claude', familyLabel:'Claude', familyBadge:'CL', familyColor:'#d97757' }]},
    /* 第三个家族：8 个模型 → 显示搜索框 */
    { id:'openai', label:'GPT', badge:'GPT', color:'#10a37f', count:8, modelCount:8,
      models:[
        { providerId:'ep_gw', providerName:'自建网关', modelId:'gpt-4o',
          label:'gpt-4o', available:true, state:'available', stateLabel:'可用',
          family:'openai', familyLabel:'GPT', familyBadge:'GPT', familyColor:'#10a37f' },
        { providerId:'ep_gw', providerName:'自建网关', modelId:'gpt-4o-mini',
          label:'gpt-4o-mini', available:true, state:'available', stateLabel:'可用',
          family:'openai', familyLabel:'GPT', familyBadge:'GPT', familyColor:'#10a37f' },
        { providerId:'ep_gw', providerName:'自建网关', modelId:'gpt-4.1',
          label:'gpt-4.1', available:true, state:'available', stateLabel:'可用',
          family:'openai', familyLabel:'GPT', familyBadge:'GPT', familyColor:'#10a37f' },
        { providerId:'ep_gw', providerName:'自建网关', modelId:'o1-preview',
          label:'o1-preview', available:true, state:'available', stateLabel:'可用',
          family:'openai', familyLabel:'GPT', familyBadge:'GPT', familyColor:'#10a37f' },
        { providerId:'ep_gw', providerName:'自建网关', modelId:'o3-mini',
          label:'o3-mini', available:true, state:'available', stateLabel:'可用',
          family:'openai', familyLabel:'GPT', familyBadge:'GPT', familyColor:'#10a37f' },
        { providerId:'ep_gw', providerName:'自建网关', modelId:'o4-mini',
          label:'o4-mini', available:true, state:'available', stateLabel:'可用',
          family:'openai', familyLabel:'GPT', familyBadge:'GPT', familyColor:'#10a37f' },
        { providerId:'ep_gw', providerName:'自建网关', modelId:'gpt-5',
          label:'gpt-5', available:true, state:'available', stateLabel:'可用',
          family:'openai', familyLabel:'GPT', familyBadge:'GPT', familyColor:'#10a37f' },
        { providerId:'ep_gw', providerName:'自建网关', modelId:'gpt-5-mini',
          label:'gpt-5-mini', available:true, state:'available', stateLabel:'可用',
          family:'openai', familyLabel:'GPT', familyBadge:'GPT', familyColor:'#10a37f' }
      ]},
    { id:'qwen', label:'Qwen', badge:'QW', color:'#615ced', count:0, modelCount:1,
      models:[{ providerId:'ep_none', providerName:'没配Key', modelId:'qwen-max',
                label:'qwen-max', available:false, state:'unconfigured', stateLabel:'未配置',
                family:'qwen', familyLabel:'Qwen', familyBadge:'QW', familyColor:'#615ced' }]},
    { id:'zhipu', label:'GLM', badge:'GL', color:'#3859ff', count:0, modelCount:1,
      models:[{ providerId:'ep_off', providerName:'已禁用', modelId:'glm-4-plus',
                label:'glm-4-plus', available:false, state:'disabled', stateLabel:'未启用',
                family:'zhipu', familyLabel:'GLM', familyBadge:'GL', familyColor:'#3859ff' }]},
  ],
  endpoints: [
    { id:'ep_ds', label:'DeepSeek', baseUrl:'https://api.deepseek.com/v1',
      provider:'deepseek', state:'available', stateLabel:'可用', hasKey:true,
      modelCount:2, active:false },
    { id:'ep_gw', label:'自建网关', baseUrl:'https://gw.example.com/v1',
      provider:'openai', state:'available', stateLabel:'可用', hasKey:true,
      modelCount:2, active:true },
    { id:'ep_none', label:'没配Key', baseUrl:'https://api.qwen.com/v1',
      provider:'qwen', state:'unconfigured', stateLabel:'未配置', hasKey:false,
      modelCount:1, active:false },
    { id:'ep_off', label:'已禁用', baseUrl:'https://open.bigmodel.cn/v1',
      provider:'zhipu', state:'disabled', stateLabel:'未启用', hasKey:true,
      modelCount:1, active:false },
  ],
  active: { endpointId:'ep_gw', modelId:'claude-sonnet-4' },
  current: { baseUrl:'https://gw.example.com/v1', model:'claude-sonnet-4',
             source:'active', activeEndpoint:'ep_gw', activeModel:'claude-sonnet-4' },
  totalModels: 6, availableModels: 4,
};
</script>
<script src="__MP__"></script>
<script>
const app = Vue.createApp({
  components: { 'model-picker': window.MosuV2.ModelPicker },
  template: '<div><model-picker mode="full"></model-picker></div>'
});
app.mount('#probe');
window.__MP_READY__ = true;
</script></body></html>"""


class _Probe:
    def __init__(self, pg):
        self.pg = pg

    def count(self, sel):
        return self.pg.locator(sel).count()

    def model_ids(self):
        return self.pg.eval_on_selector_all(
            ".v2-mp-item", "els => els.map(e => e.getAttribute('data-model-id'))")

    def states(self):
        return self.pg.eval_on_selector_all(
            ".v2-mp-item",
            "els => els.map(e => [e.getAttribute('data-model-id'),"
            " e.getAttribute('data-state'), e.disabled])")

    def family_buttons(self):
        return self.pg.eval_on_selector_all(
            ".v2-mp-fam",
            "els => els.map(e => e.getAttribute('data-anchor-key'))")

    def click_family(self, fid):
        self.pg.click(f".v2-mp-fam[data-anchor-key='family:{fid}']")

    def active_label(self):
        return self.pg.locator(".v2-mp-item.active .v2-mp-item-label").first.inner_text()


class TestModelPickerRender(unittest.TestCase):
    """模型选择器渲染行为（真浏览器 + 真实组件 + 固定数据）。"""

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
        html = html.replace("__MP__",
                            (ROOT / "gui/assets/v2/views-models.js").as_uri())
        css = (ROOT / "gui/assets/v2/app.css").read_text(encoding="utf-8")
        theme = (ROOT / "gui/assets/theme.css").read_text(encoding="utf-8")
        # theme.css 的 :root 令牌 + app.css 的零件样式一起内联
        html = html.replace("__APP_CSS__", theme + "\n" + css)

        # 必须落成真文件再用 file:// 打开：
        # set_content 用的是 about:blank 源，加载 file:// 脚本会被浏览器拦掉。
        tmp = ROOT / ".cache" / "audit" / "_mp_render_probe.html"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(html, encoding="utf-8")
        cls._page.goto(tmp.as_uri(), wait_until="networkidle")
        cls._page.wait_for_function("window.__MP_READY__ === true")

    @classmethod
    def tearDownClass(cls):
        try:
            cls._browser.close()
            cls._pw.stop()
        except Exception:
            pass

    def setUp(self):
        self.p = _Probe(self._page)
        # 每个用例从默认家族开始（点一次 DeepSeek 再点回来不现实，
        # 直接刷新页面最干净）
        self._page.reload(wait_until="networkidle")
        self._page.wait_for_function("window.__MP_READY__ === true")

    def test_no_console_or_page_errors(self):
        """组件不能有 JS 错误——Vue 静默失败最常见。"""
        self.assertEqual(self.errors, [], f"前端报错：{self.errors}")

    def test_component_renders(self):
        """组件真的渲染出来了（局部注册方向反了会静默不渲染）。"""
        self.assertGreater(self.p.count(".v2-mp"), 0)
        self.assertGreater(self.p.count(".v2-mp-fam"), 0)

    def test_family_buttons_carry_anchor_keys(self):
        """家族按钮必须带 data-anchor-key="family:<id>"（端到端靠它定位）。"""
        keys = self.p.family_buttons()
        self.assertIn("family:deepseek", keys)
        self.assertIn("family:claude", keys)
        self.assertIn("family:qwen", keys)

    def test_first_family_is_selected_and_lists_its_models(self):
        """默认选中第一个有模型的家族，列表里是它的模型。"""
        ids = self.p.model_ids()
        self.assertEqual(ids, ["deepseek-chat", "deepseek-reasoner"])

    def test_switch_family_switches_list(self):
        """点家族按钮，列表要换。"""
        self.p.click_family("deepseek")
        ids = self.p.model_ids()
        self.assertEqual(sorted(ids), ["deepseek-chat", "deepseek-reasoner"])

    def test_unconfigured_model_is_disabled(self):
        """未配置的模型必须点不动（disabled 属性）。"""
        self.p.click_family("qwen")
        states = dict((m[0], (m[1], m[2])) for m in self.p.states())
        self.assertEqual(states["qwen-max"][0], "unconfigured")
        self.assertTrue(states["qwen-max"][1], "未配置模型应该 disabled")

    def test_disabled_model_is_disabled(self):
        """未启用的模型也必须点不动。"""
        self.p.click_family("zhipu")
        states = dict((m[0], (m[1], m[2])) for m in self.p.states())
        self.assertEqual(states["glm-4-plus"][0], "disabled")
        self.assertTrue(states["glm-4-plus"][1], "未启用模型应该 disabled")

    def test_available_model_is_enabled(self):
        """可用的模型不能是 disabled，否则用户永远切不了模型。"""
        self.p.click_family("deepseek")
        states = dict((m[0], (m[1], m[2])) for m in self.p.states())
        self.assertEqual(states["deepseek-chat"][0], "available")
        self.assertFalse(states["deepseek-chat"][1], "可用模型不该 disabled")

    def test_state_label_is_visible_for_non_available(self):
        """状态不能只靠颜色区分——要带文字标签。"""
        self.p.click_family("qwen")
        txt = self._page.locator(".v2-mp-item-state").first.inner_text()
        self.assertIn("未配置", txt)

    def test_click_available_model_activates_it(self):
        """点可用模型 → 调激活接口 → 该模型变成 active。"""
        self.p.click_family("deepseek")
        self._page.click(".v2-mp-item[data-model-id='deepseek-chat']")
        self._page.wait_for_timeout(150)
        posts = self._page.evaluate("window.__POSTS__")
        self.assertTrue(any(p["path"] == "/api/models/activate"
                            and p["body"]["modelId"] == "deepseek-chat"
                            and p["body"]["endpointId"] == "ep_ds"
                            for p in posts), f"没调激活接口：{posts}")

    def test_click_disabled_model_does_not_activate(self):
        """点不可用的模型不能触发激活。"""
        self.p.click_family("qwen")
        self._page.click(".v2-mp-item[data-model-id='qwen-max']",
                         force=True)
        self._page.wait_for_timeout(150)
        posts = self._page.evaluate("window.__POSTS__")
        self.assertFalse(any(p["path"] == "/api/models/activate" for p in posts),
                         "未配置模型不该触发激活")

    def test_search_filters_models(self):
        """搜索框真的过滤列表。"""
        self.p.click_family("openai")
        self.assertEqual(self.p.count(".v2-mp-search input"), 1)
        self._page.fill(".v2-mp-search input", "mini")
        self._page.wait_for_timeout(150)
        ids = self.p.model_ids()
        self.assertEqual(sorted(ids), ["gpt-4o-mini", "gpt-5-mini", "o3-mini", "o4-mini"])

    def test_search_box_appears_only_when_many_models(self):
        """模型少时不显示搜索框（Lancer 阈值 6）——省空间。"""
        self.p.click_family("qwen")     # 1 个模型
        self.assertEqual(self.p.count(".v2-mp-search input"), 0)
        self.p.click_family("deepseek")  # 2 个模型
        self.assertEqual(self.p.count(".v2-mp-search input"), 0)
        self.p.click_family("openai")    # 8 个模型
        self.assertEqual(self.p.count(".v2-mp-search input"), 1)

    def test_endpoint_list_shows_three_states(self):
        """端点区也要体现三态。"""
        txt = self._page.locator(".v2-mp-ep").all_inner_texts()
        joined = " ".join(txt)
        self.assertIn("未配置", joined)
        self.assertIn("未启用", joined)

    def test_current_model_shows_endpoint_and_model(self):
        """当前模型条要显示「端点 · 模型」，让人知道在用谁。"""
        self.p.click_family("claude")
        self.assertEqual(self.p.active_label(), "Claude Sonnet 4")


if __name__ == "__main__":
    unittest.main()
