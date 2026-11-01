# -*- coding: utf-8 -*-
"""v2 视图模板编译校验。

为什么需要这个：Vue 模板是运行时编译的，语法错误不会在写代码时暴露，
而是等到页面渲染时才报错——而且 v1 那种"渲染期兜底"会把整页变成白屏，
排查成本极高。这里用 Vue.compile() 离线逐个校验，改完模板立刻知道有没有语法错。

跑法：python tests/test_templates.py
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VUE_PATH = ROOT / "gui/vendor/vue.global.prod.js"
FILES = ["views-home.js", "views-chat.js", "views-insight.js",
         "views-branch.js",
         "views-share.js", "views-settings.js", "shell.js"]


def extract(txt: str) -> str:
    """从视图文件里抠出 template: `...` 之间的内容。"""
    i = txt.find("template: `")
    if i < 0:
        return ""
    j = i + len("template: `")
    k = txt.find("`,\n", j)
    return txt[j:k if k > 0 else len(txt)]


def _compile_all(tpls):
    """用真实 Vue 编译器逐个编译，返回 {文件名: (ok, err)}。"""
    from playwright.sync_api import sync_playwright

    vue_src = VUE_PATH.read_text(encoding="utf-8")
    result = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.add_script_tag(content=vue_src)
        for fn, tpl in tpls.items():
            ok, err = pg.evaluate(
                "(t) => { try { Vue.compile(t); return [true, null]; }"
                " catch(e) { return [false, String(e.message || e)]; } }", tpl)
            result[fn] = (ok, err)
        b.close()
    return result


def test_v2_templates_compile():
    tpls = {}
    for fn in FILES:
        p = ROOT / "gui/assets/v2" / fn
        if not p.exists():
            continue
        t = extract(p.read_text(encoding="utf-8"))
        if t:
            tpls[fn] = t

    assert tpls, "没找到任何模板——视图文件结构变了？"

    results = _compile_all(tpls)
    bad = {fn: err for fn, (ok, err) in results.items() if not ok}

    if bad:
        # 二分定位到具体行，方便直接改
        detail = []
        from playwright.sync_api import sync_playwright
        vue_src = VUE_PATH.read_text(encoding="utf-8")
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.add_script_tag(content=vue_src)
            for fn in bad:
                lines = tpls[fn].split("\n")
                for idx in range(len(lines)):
                    trial = "\n".join(lines[:idx] + lines[idx + 1:])
                    good = pg.evaluate(
                        "(t) => { try { Vue.compile(t); return true; }"
                        " catch(e) { return false; } }", trial)
                    if good:
                        detail.append(f"{fn} 第 {idx + 1} 行: {lines[idx].strip()[:120]}")
                        break
            b.close()
        raise AssertionError(
            "模板编译失败:\n" + "\n".join(detail)
            + "\n原始错误: " + "; ".join(f"{k}={v}" for k, v in bad.items()))

    print(f"{len(tpls)} 个 v2 模板全部编译通过")


if __name__ == "__main__":
    try:
        test_v2_templates_compile()
    except AssertionError as e:
        print("FAIL:", e)
        sys.exit(1)
