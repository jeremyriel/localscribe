"""Checks for reusable vocabulary profiles and study presets."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import vocabulary as V

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        FAILURES.append(label)
        print(f"  FAIL {label} {detail}")
    else:
        print(f"  ok   {label}")


def test_profile_create_and_import():
    with tempfile.TemporaryDirectory() as tmp:
        paths = SimpleNamespace(settings_file=Path(tmp) / "settings.json")
        with patch.object(V, "PATHS", paths):
            created = V.create_profile(
                "Interview terms",
                ["Riel", "TRAILblazer", "riel"],
                settings={"language": "en", "task": "transcribe", "beam_size": 4},
            )
            check("terms are deduplicated without changing display case",
                  created["term_count"] == 2)
            check("profile settings are retained",
                  created["settings"] == {
                      "language": "en", "task": "transcribe", "beam_size": 4,
                  })
            imported = V.import_profile(
                "CSV terms", "terms.csv",
                b"\xef\xbb\xbfword,category\nRiel,name\nTRAILblazer,study\n",
            )
            check("CSV terms import as a reusable profile",
                  imported["term_count"] == 2)
            check("profile listings omit vocabulary contents",
                  "terms" not in V.list_profiles()[0])
            preset = V.create_profile(
                "English translation preset", [], settings={"language": "en", "task": "translate"}
            )
            check("settings-only study presets are supported",
                  preset["term_count"] == 0 and preset["settings"]["task"] == "translate")


def test_transcription_overrides():
    profile = {
        "target": "initial_prompt",
        "terms": ["TRAILblazer", "Riel"],
        "settings": {"language": "en", "task": "translate"},
    }
    overrides = V.transcription_overrides(
        {"initial_prompt": "Existing context", "hotwords": "UIC"},
        "Project glossary",
        profile,
    )
    check("profile terms append to the active prompt without replacing it",
          overrides.get("initial_prompt") == "Existing context, TRAILblazer, Riel")
    check("study settings override global transcription options",
          overrides.get("language") == "en" and overrides.get("task") == "translate")
    hotwords = V.transcription_overrides(
        {"hotwords": "UIC"}, "", {**profile, "target": "hotwords"}
    )
    check("hotword profiles combine with existing hotwords",
          hotwords.get("hotwords") == "UIC, TRAILblazer, Riel")
    glossary = V.transcription_overrides({}, "Project glossary", None)
    check("existing project-glossary prompt behavior is preserved",
          glossary.get("initial_prompt") == "Project glossary")
    provenance = V.redact_engine_params({
        "initial_prompt": "sensitive participant names",
        "hotwords": "sensitive acronym",
        "beam_size": 5,
    })
    check("persisted engine provenance omits vocabulary contents",
          provenance["initial_prompt"] == "[omitted]"
          and provenance["hotwords"] == "[omitted]"
          and provenance["beam_size"] == 5)


def main():
    test_profile_create_and_import()
    test_transcription_overrides()
    if FAILURES:
        print(f"\n{len(FAILURES)} vocabulary check(s) failed.")
        raise SystemExit(1)
    print("\nAll vocabulary checks passed.")


if __name__ == "__main__":
    main()
