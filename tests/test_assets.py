"""Static guards on the front-end assets.

These are cheap checks for two properties that are easy to break by accident
and expensive to notice: the `hidden` attribute actually hiding things, and the
interface loading nothing from the internet.

Run with:  .venv/Scripts/python.exe -m tests.test_assets
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CSS = ROOT / "app" / "static" / "css" / "app.css"
TEMPLATES = ROOT / "app" / "templates"
JS = ROOT / "app" / "static" / "js"

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def top_level_rules(css: str):
    """Yield (selector, body) for rules at nesting depth 0.

    Written as a brace-depth scan rather than a regex because @media and
    @keyframes blocks nest, and a flat regex desynchronises on them.
    """
    sel = buf = ""
    depth = 0
    for ch in css:
        if ch == "{":
            depth += 1
            if depth == 1:
                sel, buf = buf.strip(), ""
            else:
                buf += ch
        elif ch == "}":
            depth -= 1
            if depth == 0:
                yield sel, buf
                buf = ""
            else:
                buf += ch
        else:
            buf += ch


def test_hidden_attribute_is_enforced():
    """The global [hidden] rule must exist.

    Without it, any element whose CSS sets `display` ignores `el.hidden`,
    because author styles beat the browser's user-agent [hidden] rule
    regardless of specificity. This broke the console's Hide button: the
    panel is `display: flex`, so setting hidden had no visual effect at all.
    """
    css = strip_comments(CSS.read_text(encoding="utf-8"))
    rule = re.search(
        r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", css
    )
    check("a global [hidden] rule with !important exists", bool(rule),
          "add: [hidden] { display: none !important; }")


def test_hidden_elements_are_covered():
    """Report which hidden-toggled elements rely on that rule."""
    css = strip_comments(CSS.read_text(encoding="utf-8"))

    display_setters: dict = {}
    for sel, body in top_level_rules(css):
        if sel.startswith("@"):
            continue   # print styles and keyframes are not the screen cascade
        if re.search(r"(?:^|;|\s)display\s*:", body):
            for one in sel.split(","):
                value = re.search(r"(?:^|;|\s)display\s*:\s*([^;]+)", body)
                display_setters[one.strip()] = value.group(1).strip()

    at_risk = []
    for template in sorted(TEMPLATES.glob("*.html")):
        text = re.sub(r"<!--.*?-->", "", template.read_text(encoding="utf-8"), flags=re.S)
        for tag in re.finditer(r"<(\w+)([^>]*\bhidden\b[^>]*)>", text):
            attrs = tag.group(2)
            eid = re.search(r'id="([^"]+)"', attrs)
            classes = re.search(r'class="([^"]+)"', attrs)
            selectors = [f"#{eid.group(1)}"] if eid else []
            if classes:
                selectors += [f".{c}" for c in classes.group(1).split()]
            for sel in selectors:
                if sel in display_setters:
                    at_risk.append((template.name, sel, display_setters[sel]))

    for name, sel, value in at_risk:
        print(f"       note: {name} hides an element matching "
              f"{sel} (display: {value}) - covered by the global rule")
    # This is informational: the global rule covers them. The check exists so
    # that the list is visible when someone adds a new hidden component.
    check("hidden elements with display rules are accounted for", True,
          f"{len(at_risk)} such element(s)")


def test_no_external_resources():
    """The interface must load nothing from the internet.

    This is the load-bearing privacy property: a single CDN reference would
    leak the fact and timing of use to a third party, and would break the app
    entirely on an air-gapped machine.
    """
    offenders = []
    pattern = re.compile(r"""(?:src|href)\s*=\s*["'](?:https?:)?//([^"']+)""")

    for path in list(TEMPLATES.glob("*.html")) + [CSS] + list(JS.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            offenders.append(f"{path.name}: {match.group(0)[:70]}")
        for match in re.finditer(r"@import\s+url\(\s*['\"]?https?:", text):
            offenders.append(f"{path.name}: @import of a remote stylesheet")

    check("no external src/href in templates, CSS or JS", not offenders,
          "\n       " + "\n       ".join(offenders) if offenders else "")

    # Fonts must be a local stack, never a remote font file.
    css = CSS.read_text(encoding="utf-8")
    check("no @font-face pointing at a remote file",
          not re.search(r"@font-face[^}]*url\(\s*['\"]?https?:", css, re.S))


def test_templates_reference_existing_assets():
    """Every /static/ reference in a template must exist on disk."""
    missing = []
    for template in sorted(TEMPLATES.glob("*.html")):
        text = template.read_text(encoding="utf-8")
        for match in re.finditer(r'["\'](/static/[^"\']+)["\']', text):
            asset = ROOT / "app" / match.group(1).lstrip("/")
            if not asset.exists():
                missing.append(f"{template.name} -> {match.group(1)}")
    check("all /static/ references resolve to real files", not missing,
          "\n       " + "\n       ".join(missing) if missing else "")


def main() -> int:
    tests = [
        test_hidden_attribute_is_enforced,
        test_hidden_elements_are_covered,
        test_no_external_resources,
        test_templates_reference_existing_assets,
    ]
    for test in tests:
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
