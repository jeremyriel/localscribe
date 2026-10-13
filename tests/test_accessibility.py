"""Static accessibility baseline checks for the server-rendered interface.

Run with: python -m tests.test_accessibility
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CSS = ROOT / "app" / "static" / "css" / "app.css"
BASE = ROOT / "app" / "templates" / "base.html"
DOCUMENT = ROOT / "app" / "templates" / "document.html"
SETTINGS = ROOT / "app" / "templates" / "settings.html"
EDITOR = ROOT / "app" / "static" / "js" / "editor.js"
SPEAKERS = ROOT / "app" / "static" / "js" / "speakers.js"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def _color(css: str, name: str) -> str | None:
    match = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", css)
    return match.group(1) if match else None


def _luminance(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    a, b = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def test_landmarks_and_keyboard_entry():
    base = BASE.read_text(encoding="utf-8")
    document = DOCUMENT.read_text(encoding="utf-8")
    settings = SETTINGS.read_text(encoding="utf-8")
    check("skip link targets the main landmark",
          'href="#main-content"' in base and 'id="main-content" tabindex="-1"' in base)
    check("primary navigation has a name and active page state",
          '<nav class="nav" aria-label="Primary navigation">' in base
          and 'aria-current="page"' in base)
    check("main transcript controls expose labels and the waveform slider",
          'aria-label="Number of speakers"' in document
          and 'role="slider"' in document
          and 'aria-valuemin="0"' in document
          and 'aria-valuenow="0"' in document)
    check("non-English proofreading limitations are visible",
          "Proofreading is currently supported for English transcripts only."
          in document
          and "not validated" in settings
          and "model marked multilingual" in settings)


def test_transcript_precedes_collapsed_tool_panels():
    document = DOCUMENT.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    editor = EDITOR.read_text(encoding="utf-8")
    tools = (ROOT / "app" / "static" / "js" / "tools.js").read_text(encoding="utf-8")
    panels = ("tool-review", "tool-speakers", "tool-deid", "tool-notes", "tool-help")

    check("the transcript comes before every secondary tools panel",
          all(document.index('id="segments"') < document.index(f'id="{panel}"')
              for panel in panels))
    check("secondary tools are collapsed details with a summary",
          all(re.search(rf'<details class="settings-group" id="{panel}">\s*<summary',
                        document) for panel in panels))
    moved = ("review-queue", "review-next", "speaker-rows", "spk-count", "spk-plus",
             "spk-minus", "spk-state", "deidentification-review", "deid-scan",
             "deid-items", "annotations-panel", "annotation-add", "annotations-list")
    check("moved controls keep the ids the scripts bind to, once each",
          all(document.count(f'id="{name}"') == 1 for name in moved))
    check("each tools panel has a header link that opens it",
          all(f'href="#{panel}" data-open-tool' in document for panel in panels)
          and "a[data-open-tool]" in tools and "panel.open = true" in tools)
    check("the de-identification warning stays inside its panel and summary",
          document.index("Assistive flags only: findings can be missed")
          > document.index('id="tool-deid"')
          and "(assistive flags only)</summary>" in document)
    check("global shortcuts ignore focused panel summaries",
          editor.count("button, summary, [contenteditable") == 2)
    check("the player is sticky only where the viewport has room",
          "@media (min-width: 761px) and (min-height: 600px)" in css
          and re.search(r"\.player-card \{ position: sticky; top: 3\.6rem; \}", css))
    check("print hides the skip link and the tools column",
          re.search(r"@media print \{\s*\.skip-link,[^}]*\.side-panel", css))


def test_settings_save_controls_stay_reachable():
    settings = SETTINGS.read_text(encoding="utf-8")
    script = (ROOT / "app" / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    check("the Save button submits the settings form from the sticky bar",
          'type="submit" form="settings-form"' in settings
          and settings.index("</form>") < settings.index('id="settings-actions"'))
    check("the sticky bar is a named region with a polite status",
          'aria-label="Save or restore settings"' in settings
          and 'id="settings-state" role="status" aria-live="polite"' in settings)
    check("unsaved changes are shown with text and a visual state",
          "is-dirty" in script and "'dirty'" in script)


def test_keyboard_speaker_workflow_and_color_cues():
    editor = EDITOR.read_text(encoding="utf-8")
    speakers = SPEAKERS.read_text(encoding="utf-8")
    check("speaker assignment supports focused-turn number shortcuts",
          "focusedTurn()" in editor and "assignTurn(turnEl, speaker.id)" in editor)
    check("speaker rows include visible text labels independent of color",
          "speaker.short || speaker.name" in editor
          and "class=\"speaker-key\"" in speakers)


def test_focus_scaling_contrast_and_motion_preferences():
    css = CSS.read_text(encoding="utf-8")
    check("default type scales with the user's root font size",
          bool(re.search(r"body\s*\{[^}]*font:\s*1rem/", css, re.S)))
    check("keyboard focus is visible",
          ":focus-visible" in css and "outline-offset" in css)
    check("forced-colors mode preserves focus and control boundaries",
          "@media (forced-colors: active)" in css
          and "outline: 3px solid Highlight" in css)
    check("reduced-motion preference is respected",
          "@media (prefers-reduced-motion: reduce)" in css)

    ink = _color(css, "ink-3")
    surfaces = [_color(css, token) for token in ("paper", "surface", "surface-2")]
    check("muted text reaches 4.5:1 on standard surfaces",
          bool(ink and all(surface and _contrast(ink, surface) >= 4.5
                           for surface in surfaces)),
          f"ink-3={ink}, surfaces={surfaces}")


def main() -> int:
    for test in (
        test_landmarks_and_keyboard_entry,
        test_transcript_precedes_collapsed_tool_panels,
        test_settings_save_controls_stay_reachable,
        test_keyboard_speaker_workflow_and_color_cues,
        test_focus_scaling_contrast_and_motion_preferences,
    ):
        print(f"\n{test.__name__}")
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
