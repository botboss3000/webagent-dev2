"""Generate PWA icons for WebAgent.

Renders the Lucide "bot" glyph (the same shape as ui/favicon.svg) on the
brand's dark rounded square. The glyph is drawn supersampled (SS x) and then
downscaled with LANCZOS so the strokes are cleanly antialiased.

Run:  python ui/generate_icons.py
"""
from PIL import Image, ImageDraw
import os

SS = 4  # supersample factor (draw big, shrink down for smooth edges)

ACCENT = (125, 207, 255, 255)   # --accent  #7dcfff  (border)
BG = (13, 13, 26, 255)          # --bg-0    #0d0d1a  (dark square)
GLYPH = (192, 202, 245, 255)    # soft lavender #c0caf5 (the bot strokes)


def _draw_bot(draw, cx, cy, scale, color, stroke):
    """Draw the Lucide 'bot' glyph centred at (cx, cy).

    Lucide icons live on a 24x24 grid; we map a lucide coord (lx, ly) to a
    pixel by treating lucide (12, 12) as the centre and `scale` as px-per-unit.
    Round line caps are faked by stamping a filled dot at each segment end.
    """
    def P(lx, ly):
        return (cx + (lx - 12) * scale, cy + (ly - 12) * scale)

    w = int(round(stroke))
    cap = stroke / 2.0

    def dot(pt):
        x, y = pt
        draw.ellipse([x - cap, y - cap, x + cap, y + cap], fill=color)

    def seg(a, b):
        draw.line([a, b], fill=color, width=w)
        dot(a)
        dot(b)

    # Body: rounded rect  x4 y8 w16 h12 rx2  -> corners (4,8)-(20,20)
    x0, y0 = P(4, 8)
    x1, y1 = P(20, 20)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=2 * scale, outline=color, width=w)

    # Antenna: (12,8) -> (12,4) -> (8,4)
    seg(P(12, 8), P(12, 4))
    seg(P(12, 4), P(8, 4))

    # Side ears
    seg(P(2, 14), P(4, 14))
    seg(P(20, 14), P(22, 14))

    # Eyes
    seg(P(9, 13), P(9, 15))
    seg(P(15, 13), P(15, 15))


def create_icon(size, output_path):
    """Standard icon: bot glyph on a padded dark rounded square + accent border."""
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = s // 8
    r = s // 6  # corner radius
    draw.rounded_rectangle([pad, pad, s - pad, s - pad], radius=r, fill=BG)
    draw.rounded_rectangle(
        [pad, pad, s - pad, s - pad], radius=r, outline=ACCENT, width=max(1, s // 64)
    )

    scale = s * 0.026
    _draw_bot(draw, s / 2, s / 2, scale, GLYPH, stroke=2 * scale)

    img = img.resize((size, size), Image.LANCZOS)
    img.save(output_path, "PNG")


def create_maskable_icon(size, output_path):
    """Maskable variant: background fills the whole genui; glyph stays in the
    inner safe zone (80%) so platform masks never clip it."""
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    r = int(s * 0.12) if size > 256 else s // 12
    draw.rounded_rectangle([0, 0, s, s], radius=r, fill=BG)
    draw.rounded_rectangle([0, 0, s, s], radius=r, outline=ACCENT, width=max(1, s // 64))

    scale = s * 0.022  # a touch smaller to sit inside the safe zone
    _draw_bot(draw, s / 2, s / 2, scale, GLYPH, stroke=2 * scale)

    img = img.resize((size, size), Image.LANCZOS)
    img.save(output_path, "PNG")


if __name__ == "__main__":
    out_dir = os.path.join(os.path.dirname(__file__), "icons")
    os.makedirs(out_dir, exist_ok=True)

    sizes = {
        "icon-192x192.png": 192,
        "icon-512x512.png": 512,
    }
    for fname, sz in sizes.items():
        create_icon(sz, os.path.join(out_dir, fname))
        print(f"Created {fname}")

    create_maskable_icon(192, os.path.join(out_dir, "icon-192x192-maskable.png"))
    print("Created icon-192x192-maskable.png")
    create_maskable_icon(512, os.path.join(out_dir, "icon-512x512-maskable.png"))
    print("Created icon-512x512-maskable.png")
