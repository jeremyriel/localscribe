"""Generate Local Scribe's icon set.

The mark is a sheet of paper with a desktop computer drawn on it in ink line:
the machine writes on the page. It is drawn programmatically so every size is
rendered at its own scale rather than downsampled from one bitmap, which is
what keeps the 16 px favicon legible.

Run:  .venv/Scripts/python.exe assets/icon/make_icons.py
Outputs into app/static/icon/ and assets/icon/.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_ICON = ROOT / "app" / "static" / "icon"
ASSET_ICON = ROOT / "assets" / "icon"

# Palette. The paper is warm so it reads as paper rather than as a white card,
# and the ink is a near-black blue so it does not vibrate against the teal.
PAPER = (253, 252, 246, 255)
PAPER_EDGE = (206, 200, 182, 255)
PAPER_SHADOW = (34, 42, 53, 28)
FOLD = (232, 228, 214, 255)
INK = (28, 37, 48, 255)
SCREEN = (223, 238, 236, 255)
ACCENT = (15, 118, 110, 255)
RULE = (196, 206, 214, 255)

# Supersampling factor: draw large, then reduce, for clean diagonals.
SS = 8


def draw_icon(size: int, margin_scale: float = 1.0) -> Image.Image:
    """Draw the mark at `size` pixels square."""
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def u(value: float) -> float:
        """Convert 0..64 design units to pixels."""
        return value * s / 64.0

    tiny = size <= 24

    # ---- sheet of paper ---------------------------------------------------
    # A little narrower than tall, with a folded top-right corner.
    left, top, right, bottom = u(9), u(5), u(55), u(59)
    fold = u(11) if not tiny else u(9)

    # Soft drop shadow, offset down-right, so the sheet sits above the ground.
    d.rounded_rectangle(
        [left + u(1.5), top + u(2), right + u(1.5), bottom + u(2)],
        radius=u(2), fill=PAPER_SHADOW,
    )

    sheet = [
        (left, top),
        (right - fold, top),
        (right, top + fold),
        (right, bottom),
        (left, bottom),
    ]
    d.polygon(sheet, fill=PAPER)
    d.line(sheet + [sheet[0]], fill=PAPER_EDGE, width=max(1, int(u(0.7))), joint="curve")

    # The turned-down corner: a small triangle shaded darker than the sheet.
    d.polygon(
        [(right - fold, top), (right - fold, top + fold), (right, top + fold)],
        fill=FOLD,
    )
    d.line(
        [(right - fold, top), (right - fold, top + fold), (right, top + fold)],
        fill=PAPER_EDGE, width=max(1, int(u(0.7))),
    )

    # ---- the computer drawn on the page ----------------------------------
    stroke = max(1, int(u(2.6 if tiny else 2.2)))

    # Monitor.
    m_left, m_top, m_right, m_bottom = u(17), u(14), u(47), u(35)
    d.rounded_rectangle(
        [m_left, m_top, m_right, m_bottom],
        radius=u(2.5), fill=SCREEN, outline=INK, width=stroke,
    )

    if not tiny:
        # Two lines of "text" on the screen: this machine is writing words.
        inset = u(4.5)
        line_w = max(1, int(u(1.8)))
        y1 = m_top + u(7)
        y2 = m_top + u(12.5)
        d.line([m_left + inset, y1, m_right - inset, y1], fill=ACCENT, width=line_w)
        d.line([m_left + inset, y2, m_right - inset * 2.1, y2], fill=ACCENT, width=line_w)

    # Stand: neck and base.
    neck_w = u(4)
    cx = (m_left + m_right) / 2
    d.line([cx, m_bottom, cx, m_bottom + u(5)], fill=INK, width=max(1, int(neck_w)))
    d.line(
        [cx - u(7), m_bottom + u(5.5), cx + u(7), m_bottom + u(5.5)],
        fill=INK, width=stroke,
    )

    # Keyboard: a rounded slab below the stand, angled edge suggested by width.
    k_left, k_top, k_right, k_bottom = u(14), u(45), u(50), u(51)
    d.rounded_rectangle(
        [k_left, k_top, k_right, k_bottom],
        radius=u(1.6), fill=PAPER, outline=INK, width=stroke,
    )
    if not tiny:
        # Key row hint.
        d.line(
            [k_left + u(3), (k_top + k_bottom) / 2, k_right - u(3), (k_top + k_bottom) / 2],
            fill=RULE, width=max(1, int(u(1.4))),
        )

    # ---- ruled lines under the drawing -----------------------------------
    if not tiny:
        for index in range(2):
            y = u(54) + index * u(3.2)
            end = right - u(5) if index == 0 else right - u(14)
            d.line([left + u(5), y, end, y], fill=RULE, width=max(1, int(u(1.3))))

    return img.resize((size, size), Image.LANCZOS)


def write_svg(path: Path) -> None:
    """Hand-authored SVG matching the raster mark, for crisp UI rendering."""
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"
     width="64" height="64" role="img" aria-label="Local Scribe">
  <title>Local Scribe</title>
  <desc>A desktop computer drawn on a sheet of paper.</desc>
  <defs>
    <clipPath id="ls-sheet">
      <path d="M9 5 H44 L55 16 V59 H9 Z"/>
    </clipPath>
  </defs>

  <!-- sheet of paper with a turned-down corner -->
  <path d="M10.5 7 H45.5 L56.5 18 V61 H10.5 Z" fill="#222a35" opacity="0.11"/>
  <path d="M9 5 H44 L55 16 V59 H9 Z" fill="#fdfcf6" stroke="#cec8b6" stroke-width="0.9"/>
  <path d="M44 5 V16 H55 Z" fill="#e8e4d6" stroke="#cec8b6" stroke-width="0.9"
        stroke-linejoin="round"/>

  <g clip-path="url(#ls-sheet)">
    <!-- monitor -->
    <rect x="17" y="14" width="30" height="21" rx="2.5"
          fill="#dfeeec" stroke="#1c2530" stroke-width="2.2"/>
    <g stroke="#0f766e" stroke-width="1.8" stroke-linecap="round">
      <line x1="21.5" y1="21" x2="42.5" y2="21"/>
      <line x1="21.5" y1="26.5" x2="37.5" y2="26.5"/>
    </g>

    <!-- stand -->
    <path d="M32 35 V40" stroke="#1c2530" stroke-width="4" stroke-linecap="butt"/>
    <path d="M25 40.5 H39" stroke="#1c2530" stroke-width="2.2" stroke-linecap="round"/>

    <!-- keyboard -->
    <rect x="14" y="45" width="36" height="6" rx="1.6"
          fill="#fdfcf6" stroke="#1c2530" stroke-width="2.2"/>
    <line x1="17" y1="48" x2="47" y2="48" stroke="#c4ced6" stroke-width="1.4"
          stroke-linecap="round"/>

    <!-- ruled lines -->
    <g stroke="#c4ced6" stroke-width="1.3" stroke-linecap="round">
      <line x1="14" y1="54" x2="50" y2="54"/>
      <line x1="14" y1="57.2" x2="41" y2="57.2"/>
    </g>
  </g>
</svg>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")


def main() -> int:
    STATIC_ICON.mkdir(parents=True, exist_ok=True)
    ASSET_ICON.mkdir(parents=True, exist_ok=True)

    sizes = (16, 24, 32, 48, 64, 128, 180, 256, 512, 1024)
    images = {}
    for size in sizes:
        image = draw_icon(size)
        images[size] = image
        image.save(STATIC_ICON / f"icon-{size}.png")

    # Multi-resolution .ico for Windows and the browser tab.
    images[256].save(
        STATIC_ICON / "favicon.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    # macOS .icns, written only when Pillow supports it on this platform.
    try:
        images[1024].save(ASSET_ICON / "icon.icns", format="ICNS")
    except Exception as exc:
        print(f"note: .icns not written ({exc}); PNG set is available instead")

    images[1024].save(ASSET_ICON / "icon-1024.png")
    write_svg(STATIC_ICON / "icon.svg")
    write_svg(ASSET_ICON / "icon.svg")

    print(f"wrote {len(sizes)} PNG sizes, favicon.ico and icon.svg to {STATIC_ICON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
