# -*- coding: utf-8 -*-
"""品牌色对比度不变量（WCAG 2.1 相对亮度实算）。

为什么要有这个文件：`.btn-primary` 拿 `--grad-brand` 渐变配 `--brand-text:#fff`，
四个色标里只要有一个低于 4.5:1，按钮正文就「看起来有点灰」——而这种缺陷
不会报错、不会被截图评审抓到，只会被用户当成审美问题报回来。

历史教训：品牌绿曾是 `#0e9e56`，对白字只有 3.47:1，渐变里最亮那档
`#34cf85` 只有 2.02:1。这是既存的可读性缺陷，不是新配色。

**测试必须解析 theme.css 里的实际色值，不能硬编码期望值** —— 否则改色的
那只手和断言的那只手是同一只手，测出来的永远是「通过」。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THEME = ROOT / "gui" / "assets" / "theme.css"
LOGO = ROOT / "gui" / "assets" / "logo.svg"


# --------------------------------------------------------------- WCAG 相对亮度
def _srgb_to_linear(c: float) -> float:
    """sRGB 电平 → 线性光。低于 0.04045 的分段是 WCAG 2.1 的硬规定。"""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb) -> float:
    """L = 0.2126R + 0.7152G + 0.0722B，R/G/B 为 0~1 的线性光。"""
    r, g, b = (_srgb_to_linear(v / 255.0) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg, bg) -> float:
    """对比度 (L1+0.05)/(L2+0.05)，fg/bg 接受 (r,g,b) 或 "#rrggbb"。"""
    l1, l2 = sorted(
        (relative_luminance(parse_color(c)) for c in (fg, bg)), reverse=True
    )
    return (l1 + 0.05) / (l2 + 0.05)


def parse_color(value: str):
    """#rgb / #rrggbb → (r,g,b)。不接受 rgb() / 具名色：本文件只测设计令牌。"""
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", v):
        raise ValueError(f"无法解析的颜色值：{value!r}")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


# ------------------------------------------------------------- theme.css 解析
def _split_blocks(css: str):
    """按顶层 `{ }` 切块，返回 [(选择器, 块内容), ...]。注释先剥掉。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, i, buf = [], 0, ""
    while i < len(css):
        ch = css[i]
        if ch == "{":
            sel = buf.strip()
            depth, j, body = 1, i + 1, []
            while j < len(css) and depth:
                if css[j] == "{":
                    depth += 1
                elif css[j] == "}":
                    depth -= 1
                    if not depth:
                        break
                body.append(css[j])
                j += 1
            out.append((sel, "".join(body)))
            buf, i = "", j + 1
            continue
        buf += ch
        i += 1
    return out


def _tokens(css: str):
    """块内容 → {令牌名: 值}。"""
    out = {}
    for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", css):
        out[name] = value.strip()
    return out


def load_theme() -> dict:
    """返回 {"light": {...}, "dark": {...}}。选择器按块归属归类。"""
    light, dark = {}, {}
    for sel, body in _split_blocks(THEME.read_text(encoding="utf-8")):
        sels = [s.strip() for s in sel.split(",")]
        tv = _tokens(body)
        if any(s == '[data-theme="dark"]' for s in sels):
            dark.update(tv)
        elif any(s in (":root", '[data-theme="light"]') for s in sels):
            light.update(tv)
    return {"light": light, "dark": dark}


def gradient_stops(token_value: str):
    """从 linear-gradient(...) 里取出全部 #rrggbb 色标（按出现顺序）。"""
    stops = re.findall(r"#[0-9a-fA-F]{6}", token_value)
    if not stops:
        raise AssertionError(f"渐变里没有解析到色标：{token_value!r}")
    return stops


# ------------------------------------------------------------------- 断言集
class TestBrandContrast(unittest.TestCase):
    """所有断言的期望值都是从 theme.css 实际色值算出来的，不是写死的。"""

    @classmethod
    def setUpClass(cls):
        cls.theme = load_theme()
        for side in ("light", "dark"):
            assert cls.theme[side], f"theme.css 的 {side} 块解析为空，解析器坏了"

    # ---------------------------------------------------------- 浅色主题
    def test_light_brand_on_white_text(self):
        """主色纯色块（按钮、链接、强调文字）配白字必须过 AA 正文线 4.5:1。"""
        r = contrast(self.theme["light"]["--brand"], "#ffffff")
        self.assertGreaterEqual(
            r, 4.5, f"浅色 --brand 对白字仅 {r:.2f}:1，达不到 WCAG AA")

    def test_light_brand_text_token_is_white(self):
        """--brand-text 是主色上的前景色。它变深会让上面的断言失去意义，
        所以正向断言：必须是纯白，否则一并失败。"""
        self.assertEqual(parse_color(self.theme["light"]["--brand-text"]), (255, 255, 255))

    def test_light_gradient_every_stop_on_white_text(self):
        """`.btn-primary` 的四个色标逐一都要能扛白字 —— 渐变里最亮那档
        往往才是真正决定观感的那一档，不能只测首尾。"""
        stops = gradient_stops(self.theme["light"]["--grad-brand"])
        self.assertGreaterEqual(len(stops), 3, "渐变色标少于 3 档，渐变还有必要吗")
        for s in stops:
            r = contrast(s, "#ffffff")
            self.assertGreaterEqual(
                r, 4.5, f"浅色 --grad-brand 色标 {s} 对白字仅 {r:.2f}:1")

    def test_light_brand_hover_on_white_text(self):
        """悬停态更深，天然更安全，但同样锁住：防止有人把 hover 调浅。"""
        r = contrast(self.theme["light"]["--brand-hover"], "#ffffff")
        self.assertGreaterEqual(r, 4.5, f"浅色 --brand-hover 对白字仅 {r:.2f}:1")

    def test_light_focus_ring_vs_page_bg(self):
        """焦点环是非文本元素，WCAG 的门槛是 3:1（而非 4.5:1）。"""
        r = contrast(self.theme["light"]["--border-focus"], self.theme["light"]["--bg-app"])
        self.assertGreaterEqual(
            r, 3.0, f"浅色焦点环对页面底色仅 {r:.2f}:1，键盘用户看不见焦点位置")

    def test_light_focus_ring_matches_brand(self):
        """焦点环与主色同源：两套绿会让人以为焦点是另一种交互状态。"""
        self.assertEqual(
            self.theme["light"]["--border-focus"], self.theme["light"]["--brand"])

    def test_light_text_on_surface(self):
        """正文对卡片底色 —— 浅色主题真正被日常阅读的那一组。"""
        for fg in ("--text-1", "--text-2", "--text-3"):
            r = contrast(self.theme["light"][fg], self.theme["light"]["--bg-surface"])
            self.assertGreaterEqual(
                r, 4.5, f"浅色 {fg} 对卡片底色仅 {r:.2f}:1")

    # ---------------------------------------------------------- 深色主题
    def test_dark_brand_on_dark_text(self):
        """深色主题的前景色是近黑 #0d1117（不是纯白：深底上纯白会眩光）。"""
        r = contrast(self.theme["dark"]["--brand"], self.theme["dark"]["--brand-text"])
        self.assertGreaterEqual(
            r, 4.5, f"深色 --brand 对 {self.theme['dark']['--brand-text']} 仅 {r:.2f}:1")

    def test_dark_brand_text_token_is_near_black(self):
        self.assertEqual(
            parse_color(self.theme["dark"]["--brand-text"]), (13, 17, 23))

    def test_dark_gradient_every_stop_on_dark_text(self):
        stops = gradient_stops(self.theme["dark"]["--grad-brand"])
        for s in stops:
            r = contrast(s, self.theme["dark"]["--brand-text"])
            self.assertGreaterEqual(
                r, 4.5, f"深色 --grad-brand 色标 {s} 对深色前景仅 {r:.2f}:1")

    def test_dark_text_on_surface(self):
        for fg in ("--text-1", "--text-2", "--text-3"):
            r = contrast(self.theme["dark"][fg], self.theme["dark"]["--bg-surface"])
            self.assertGreaterEqual(
                r, 4.5, f"深色 {fg} 对卡片底色仅 {r:.2f}:1")


class TestBrandCohesion(unittest.TestCase):
    """品牌绿统一：同一族令牌不许各绿各的。"""

    @classmethod
    def setUpClass(cls):
        cls.theme = load_theme()

    def test_no_legacy_light_brand_green(self):
        """`#0e9e56` 对白字只有 3.47:1，是被替换掉的那个值。
        任何令牌再出现它，说明有人从旧版本把色值抄了回来。"""
        for side, tokens in self.theme.items():
            for name, value in tokens.items():
                self.assertNotIn(
                    "#0e9e56", value.lower(),
                    f"{side} 的 {name} 里出现了被弃用的旧品牌绿 {value!r}")

    def test_brand_derived_tokens_share_the_brand_rgb(self):
        """--brand-soft / --glow-brand / --grad-brand 的 rgba 与品牌纯色同源。
        改了 --brand 的色相却忘了同步这三个，界面会出现两套绿。"""
        brand = parse_color(self.theme["light"]["--brand"])
        rgb_str = f"{brand[0]},{brand[1]},{brand[2]}"
        for name in ("--brand-soft", "--glow-brand", "--grad-brand-soft"):
            self.assertIn(
                rgb_str, self.theme["light"][name].replace(" ", ""),
                f"浅色 {name} 未与 --brand 同步 rgb（{self.theme['light'][name]!r}）")

    def test_heat_ladder_is_monotonic(self):
        """热力图 0→5 是活跃度递增的语义色阶，相邻两档必须方向一致，
        否则「聊得更多」在日历上看起来和「没聊」一样。
        浅色主题由浅到深（亮度递减），深色主题由深到浅（亮度递增）——
        两边方向相反是刻意的：都是「越活跃越跳」。"""
        for side in ("light", "dark"):
            lumas = [
                relative_luminance(parse_color(self.theme[side][f"--heat-{i}"]))
                for i in range(6)
            ]
            expect_darker = side == "light"
            for i in range(5):
                step = lumas[i + 1] - lumas[i]
                self.assertGreater(
                    abs(step), 0.005,
                    f"{side} 的 --heat-{i} 与 --heat-{i + 1} 亮度几乎相同，分不出档位")
                if expect_darker:
                    self.assertLess(step, 0, f"浅色热力色阶应单调变深，第 {i}→{i + 1} 档反了")
                else:
                    self.assertGreater(step, 0, f"深色热力色阶应单调变亮，第 {i}→{i + 1} 档反了")

    def test_heat_zero_near_page_background(self):
        """「无消息」那一档必须贴近页面底色 —— 它占日历绝大多数格子，
        画得太重会让整年看起来都很忙。"""
        for side in ("light", "dark"):
            r = contrast(self.theme[side]["--heat-0"], self.theme[side]["--bg-app"])
            self.assertLess(r, 1.35, f"{side} 的 --heat-0 对底色 {r:.2f}:1，空格子太显眼")

    def test_heat_five_clearly_differs_from_zero(self):
        """「极活跃」与「无消息」必须一眼可分（实算 4.6:1 / 8.7:1）。"""
        for side in ("light", "dark"):
            r = contrast(self.theme[side]["--heat-5"], self.theme[side]["--heat-0"])
            self.assertGreater(r, 3.0, f"{side} 的热力色阶两端仅 {r:.2f}:1，跨度不够")

    def test_logo_untouched(self):
        """logo 是静态资产，渲成 PNG/ICO 后进了 favicon／快捷方式／任务栏。
        D2 的定稿是「logo 一字不动，让令牌向 logo 靠拢」——
        这条断言就是为了防止有人反过来去改 logo。"""
        svg = LOGO.read_text(encoding="utf-8")
        self.assertIn("#07c160", svg.lower(), "logo.svg 不再含微信绿 #07c160")


if __name__ == "__main__":
    unittest.main()
