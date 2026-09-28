"""Generate the 1200x630 Open Graph / Twitter social-share card.

One-off asset generator (not imported by the app). Output:
    ui/icons/og-card.png   ← referenced by app/main.py `_SEO_PREVIEW_IMAGE`

Re-run after a brand/colour change:
    .venv/Scripts/python.exe scripts/make_og_card.py

Design: warm-dark background + soft amber glow, the square app icon as the brand
mark, the "WebAgent" wordmark ("web" white, "Agent" amber), the product tagline,
and a small capability strip. Colours mirror the design-system DARK palette
(--bg-0 #16100b, --brand #e0a35e, --fg-1/-3). Requires Pillow.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "ui" / "icons" / "icon-512x512.png"
OUT = ROOT / "ui" / "icons" / "og-card.png"

W, H = 1200, 630
BG_TOP = (24, 17, 11)        # warm dark, near --bg-0
BG_BOT = (12, 9, 6)
AMBER = (224, 163, 94)       # --brand
WHITE = (245, 237, 226)      # --fg-1 (warm white)
MUTED = (176, 160, 142)      # --fg-3
PAD = 96


def _font(names, size):
    for n in names:
        for base in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts")):
            p = base / n
            if p.is_file():
                try:
                    return ImageFont.truetype(str(p), size)
                except Exception:
                    pass
    return ImageFont.load_default()


BOLD = ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"]
REG = ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"]


def main():
    # Vertical gradient base.
    grad = Image.new("RGB", (1, H))
    gp = grad.load()
    for y in range(H):
        t = y / (H - 1)
        gp[0, y] = tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3))
    img = grad.resize((W, H))

    # Soft amber glow, upper-right, blurred.
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([W - 560, -260, W + 200, 360], fill=(120, 78, 30))
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    img = Image.blend(img, Image.composite(glow, img, glow.convert("L")), 0.55)

    draw = ImageDraw.Draw(img)

    # Brand mark (square app icon), top-left of the content box.
    y = PAD
    if ICON.is_file():
        mark = Image.open(ICON).convert("RGBA").resize((132, 132), Image.LANCZOS)
        img.paste(mark, (PAD, y), mark)

    # Wordmark, just below the mark.
    y += 132 + 40
    wf = _font(BOLD, 120)
    web_w = draw.textlength("web", font=wf)
    draw.text((PAD, y), "web", font=wf, fill=WHITE)
    draw.text((PAD + web_w, y), "Agent", font=wf, fill=AMBER)

    # Tagline.
    y += 150
    draw.text((PAD, y), "Your own team of AI agents that get things done.",
              font=_font(REG, 44), fill=MUTED)

    # Capability strip near the bottom.
    draw.text((PAD, H - PAD - 4), "Chat   ·   Automate   ·   Browse   ·   Build   ·   Share",
              font=_font(REG, 30), fill=AMBER)

    img.save(OUT, "PNG")
    print(f"wrote {OUT}  ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
