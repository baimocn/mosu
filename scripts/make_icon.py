"""生成墨溯图标（一次性资产生成，不进仓库主代码目录）。"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(r"D:\Desktop\墨溯\gui\assets\mosu.ico")
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
BG = (7, 193, 96, 255)          # 微信绿
BG2 = (5, 160, 80, 255)         # 深一档，做渐变底


def find_font(size: int):
    for p in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
              r"C:\Windows\Fonts\simhei.ttf"):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default(size)


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 圆角矩形底（微信绿竖向渐变）
    r = int(size * 0.22)
    for y in range(size):
        t = y / max(1, size - 1)
        c = tuple(int(BG[i] * (1 - t) + BG2[i] * t) for i in range(4))
        d.line([(0, y), (size, y)], fill=c)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    img.putalpha(mask)
    # 白色「溯」字——取水回溯之意
    f = find_font(int(size * 0.62))
    text = "溯"
    bb = d.textbbox((0, 0), text, font=f)
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    d.text(((size - w) / 2 - bb[0], (size - h) / 2 - bb[1] - size * 0.02),
           text, font=f, fill=(255, 255, 255, 255))
    return img


def main() -> None:
    base = render(256)
    base.save(OUT, format="ICO",
              sizes=[(s, s) for s in SIZES],
              bitmap_format="png")
    print(f"icon -> {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
