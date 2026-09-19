"""Generate the GitHub repo social-preview card (og:image).

GitHub uses this image (Settings -> General -> Social preview, 1280x640
recommended) as the link-unfurl thumbnail on LinkedIn, Facebook, Slack,
Twitter/X, etc. Without one set, GitHub auto-generates a generic card that
can pull in the owner's avatar - this replaces that with the app's own logo,
name and tagline.

Run:  .venv/Scripts/python.exe assets/social/make_social_card.py
Outputs: assets/social/social-preview.png (upload this by hand at
GitHub.com -> repo -> Settings -> General -> Social preview -> Edit -> the
image itself can't be set via git/API, only through that page).
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = Path(__file__).resolve().parent
LOGO = ROOT / "assets" / "icon" / "icon-1024.png"

W, H = 1280, 640

PAPER = (253, 252, 246, 255)
LINE = (221, 217, 204, 255)
INK = (28, 37, 48, 255)
INK_2 = (65, 76, 89, 255)
ACCENT = (15, 118, 110, 255)
ACCENT_SOFT = (223, 238, 236, 255)

FONT_DIR = Path("/System/Library/Fonts/Supplemental")
TITLE_FONT = FONT_DIR / "Arial Bold.ttf"
BODY_FONT = FONT_DIR / "Arial.ttf"


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def wrap_to_width(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=fnt) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def main() -> int:
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)

    # Thin accent rule along the top edge.
    d.rectangle([0, 0, W, 6], fill=ACCENT)

    # ---- logo, left side, on a soft rounded card ---------------------
    card_size = 400
    card_x, card_y = 84, (H - card_size) // 2
    d.rounded_rectangle(
        [card_x, card_y, card_x + card_size, card_y + card_size],
        radius=28, fill=ACCENT_SOFT, outline=LINE, width=2,
    )
    logo = Image.open(LOGO).convert("RGBA")
    pad = 44
    inner = card_size - pad * 2
    logo = logo.resize((inner, inner), Image.LANCZOS)
    img.paste(logo, (card_x + pad, card_y + pad), logo)

    # ---- text, right side ----------------------------------------------
    text_x = card_x + card_size + 72
    text_max_width = W - text_x - 72

    title_fnt = font(TITLE_FONT, 76)
    d.text((text_x, 168), "Local Scribe", font=title_fnt, fill=INK)

    tag_fnt = font(BODY_FONT, 33)
    tagline = (
        "Free and open-source AI-based automated audio transcription, "
        "captioning, and easy transcript editing software for researchers."
    )
    lines = wrap_to_width(d, tagline, tag_fnt, text_max_width)
    y = 268
    line_h = 46
    for line in lines:
        d.text((text_x, y), line, font=tag_fnt, fill=INK_2)
        y += line_h

    # Small footer chip: offline / local-first badge.
    chip_fnt = font(BODY_FONT, 26)
    chip_text = "Runs entirely on your machine — offline, private, open-source"
    y += 20
    d.text((text_x, y), chip_text, font=chip_fnt, fill=ACCENT)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "social-preview.png"
    img.save(out_path, format="PNG", optimize=True)
    print(f"wrote {out_path} ({W}x{H})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
