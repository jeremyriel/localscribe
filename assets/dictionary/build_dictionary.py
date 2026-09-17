"""Build Local Scribe's spelling dictionary from the SCOWL Hunspell source.

The upstream `.dic` lists ~49,500 root words annotated with affix flags, and
the `.aff` file holds the rules those flags refer to. Hunspell applies them at
lookup time; Local Scribe expands them once, here, so that the application
only ever does a set membership test. That keeps `app/proofread.py` free of
morphology code and makes checking a 2,000-segment transcript cheap.

The expanded list is written gzipped, because the plain text is ~1.7 MB and
compresses to roughly a quarter of that.

Run (needs network, once):

    .venv/Scripts/python.exe assets/dictionary/build_dictionary.py

American and British lists are merged, because the checker cannot know which
variety a transcript uses and a wrongly flagged "programme" is far more costly
than an unflagged spelling inconsistency.

Source: https://github.com/wooorm/dictionaries (en, en-GB) which packages
SCOWL by Kevin Atkinson. SCOWL is BSD-style licensed; its full text is
vendored beside the output as SCOWL-LICENSE.txt and must ship with any
redistribution.
"""

from __future__ import annotations

import gzip
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = ROOT / "app" / "data" / "dictionary"

BASE = "https://raw.githubusercontent.com/wooorm/dictionaries/main/dictionaries"

# Both American and British spellings are merged into one list.
#
# The application cannot know which variety a given transcript uses: Whisper
# produces whichever it decides on, participants may use either, and a US lab
# still quotes British sources. Flagging "programme", "organisation" or
# "behaviour" as misspelled would be a constant, wrong distraction, and a
# false positive costs far more here than a missed US/UK inconsistency -- which
# is a house-style question, not a spelling error.
VARIETIES = ("en", "en-GB")


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_affix(text: str) -> dict:
    """Parse SFX/PFX blocks into {flag: {"kind", "cross", "rules": [...]}}.

    A rule is (strip, append, condition_regex). Hunspell conditions are
    character-class patterns anchored at the end of the word for suffixes and
    the start for prefixes.
    """
    table: dict = {}
    current_flag = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] not in ("SFX", "PFX"):
            continue

        kind, flag = parts[0], parts[1]

        # Header line: "SFX D Y 4"
        if len(parts) == 4 and parts[2] in ("Y", "N") and parts[3].isdigit():
            table[flag] = {"kind": kind, "cross": parts[2] == "Y", "rules": []}
            current_flag = flag
            continue

        # Rule line: "SFX D   y     ied        [^aeiou]y"
        if len(parts) >= 4 and flag in table:
            strip = "" if parts[2] == "0" else parts[2]
            append = "" if parts[3] == "0" else parts[3]
            # Strip any morphological fields after the condition.
            condition = parts[4] if len(parts) > 4 else "."
            table[flag]["rules"].append((strip, append, condition))
            current_flag = flag

    return table


def condition_matches(word: str, condition: str, kind: str) -> bool:
    if condition == ".":
        return True
    pattern = condition if kind == "PFX" else condition + "$"
    if kind == "PFX":
        pattern = "^" + condition
    try:
        return re.search(pattern, word) is not None
    except re.error:
        return False


def apply_rule(word: str, strip: str, append: str, kind: str) -> str | None:
    if kind == "SFX":
        if strip and not word.endswith(strip):
            return None
        stem = word[: len(word) - len(strip)] if strip else word
        return stem + append
    if strip and not word.startswith(strip):
        return None
    stem = word[len(strip):] if strip else word
    return append + stem


def expand(dic_text: str, affixes: dict) -> set:
    words: set = set()
    lines = dic_text.splitlines()
    # The first line is the entry count, not a word.
    if lines and lines[0].strip().isdigit():
        lines = lines[1:]

    for raw in lines:
        entry = raw.strip()
        if not entry:
            continue

        # "word/FLAGS" - flags are single characters, possibly with a trailing
        # morphological field separated by a tab.
        entry = entry.split("\t", 1)[0]
        if "/" in entry:
            word, flags = entry.split("/", 1)
        else:
            word, flags = entry, ""

        word = word.strip()
        if not word:
            continue
        words.add(word)

        suffixed = {word}
        # Suffixes first, so prefixes can later apply to the suffixed forms
        # where the flag pair allows cross-products.
        for flag in flags:
            spec = affixes.get(flag)
            if not spec or spec["kind"] != "SFX":
                continue
            for strip, append, condition in spec["rules"]:
                if not condition_matches(word, condition, "SFX"):
                    continue
                form = apply_rule(word, strip, append, "SFX")
                if form:
                    words.add(form)
                    if spec["cross"]:
                        suffixed.add(form)

        for flag in flags:
            spec = affixes.get(flag)
            if not spec or spec["kind"] != "PFX":
                continue
            targets = suffixed if spec["cross"] else {word}
            for target in targets:
                for strip, append, condition in spec["rules"]:
                    if not condition_matches(target, condition, "PFX"):
                        continue
                    form = apply_rule(target, strip, append, "PFX")
                    if form:
                        words.add(form)

    return words


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Downloading SCOWL sources...")

    words: set = set()
    licence = ""
    try:
        for variety in VARIETIES:
            dic_text = fetch(f"{BASE}/{variety}/index.dic")
            aff_text = fetch(f"{BASE}/{variety}/index.aff")
            if not licence:
                licence = fetch(f"{BASE}/{variety}/license")

            affixes = parse_affix(aff_text)
            roots = sum(1 for line in dic_text.splitlines()[1:] if line.strip())
            expanded = expand(dic_text, affixes)
            added = len(expanded - words)
            words |= expanded
            print(f"  {variety}: {roots:,} roots -> {len(expanded):,} forms "
                  f"({added:,} new)")
    except Exception as exc:
        print(f"  download failed: {exc}", file=sys.stderr)
        print("  This script needs network access. It is run once by a "
              "maintainer; the built dictionary ships with the application.",
              file=sys.stderr)
        return 1

    print(f"  merged: {len(words):,} word forms")

    # Fold to lowercase for the lookup set, but keep any form that is only
    # ever capitalised (proper nouns) so "Tuesday" is not accepted as "tuesday"
    # being wrong -- the checker is case-insensitive, so one lowercase set is
    # enough and keeps the file small.
    lowered = sorted({w.lower() for w in words if w})
    print(f"  {len(lowered):,} unique lowercase forms")

    target = OUT_DIR / "en_US.txt.gz"
    # newline="" disables the platform newline translation that text mode would
    # otherwise apply. Without it a Windows build writes CRLF into the data
    # file and the artefact differs depending on which machine produced it.
    with gzip.open(target, "wt", encoding="utf-8", newline="",
                   compresslevel=9) as handle:
        handle.write("\n".join(lowered))
    print(f"  wrote {target} ({target.stat().st_size / 1024:.0f} KB)")

    licence_path = OUT_DIR / "SCOWL-LICENSE.txt"
    licence_path.write_text(licence, encoding="utf-8")
    print(f"  wrote {licence_path} ({licence_path.stat().st_size / 1024:.0f} KB)")

    for probe in ("transcribe", "transcribed", "unhappy", "reconsidered",
                  "children", "don't", "teacher's",
                  "programme", "organisation", "behaviour", "analyse",
                  "program", "organization", "behavior", "analyze"):
        print(f"    {probe!r:16} present: {probe.lower() in set(lowered)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
