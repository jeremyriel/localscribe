"""Generate Local Scribe's icon set from the app's logo artwork.

The source of truth is assets/icon/logo-source.jpeg - a robot hand writing
with a quill on a scroll. Every output size is resized from that one raster
source with Lanczos resampling; there is no vector mark to redraw at each
scale (unlike the original programmatic icon this script used to draw), so
small sizes are just careful downsamples of a high-resolution source.

Run:  .venv/Scripts/python.exe assets/icon/make_icons.py
Outputs into app/static/icon/ and assets/icon/.
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_ICON = ROOT / "app" / "static" / "icon"
ASSET_ICON = ROOT / "assets" / "icon"
SOURCE = ASSET_ICON / "logo-source.jpeg"

# Logo art sits on a plain white background with a margin of white padding
# around it; trim that margin so the mark fills each generated icon the same
# way a purpose-drawn icon would, then re-pad to a perfect square.
TRIM_THRESHOLD = 250  # pixel considered "background" if all channels >= this


def load_source() -> Image.Image:
    img = Image.open(SOURCE).convert("RGB")

    # Trim the white margin down to the artwork's bounding box.
    gray = img.convert("L")
    bbox = gray.point(lambda p: 0 if p >= TRIM_THRESHOLD else 255).getbbox()
    if bbox:
        img = img.crop(bbox)

    # Pad back out to a square, centered, on white, with a small margin so
    # the mark doesn't touch the edge of the icon.
    w, h = img.size
    side = max(w, h)
    margin = int(side * 0.06)
    canvas_side = side + margin * 2
    canvas = Image.new("RGB", (canvas_side, canvas_side), (255, 255, 255))
    canvas.paste(img, ((canvas_side - w) // 2, (canvas_side - h) // 2))
    return canvas


def write_svg(path: Path, png_1024: Image.Image) -> None:
    """SVG wrapper embedding the raster mark, for crisp small inline use."""
    buf = io.BytesIO()
    png_1024.resize((256, 256), Image.LANCZOS).save(buf, format="PNG", optimize=True)
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"
     width="64" height="64" role="img" aria-label="Local Scribe">
  <title>Local Scribe</title>
  <desc>A robot hand writing with a quill on a scroll.</desc>
  <image href="{data_uri}" x="0" y="0" width="64" height="64"
         preserveAspectRatio="xMidYMid meet"/>
</svg>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")


def main() -> int:
    STATIC_ICON.mkdir(parents=True, exist_ok=True)
    ASSET_ICON.mkdir(parents=True, exist_ok=True)

    source = load_source()

    sizes = (16, 24, 32, 48, 64, 128, 180, 256, 512, 1024)
    images = {}
    for size in sizes:
        image = source.resize((size, size), Image.LANCZOS)
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
    write_svg(STATIC_ICON / "icon.svg", images[1024])
    write_svg(ASSET_ICON / "icon.svg", images[1024])

    print(f"wrote {len(sizes)} PNG sizes, favicon.ico and icon.svg to {STATIC_ICON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
