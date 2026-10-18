"""Standalone checks for local reversible de-identification review assistance."""

from __future__ import annotations

import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import config, deidentification, handoff, projects
from app.transcript import make_segment, new_document

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


@contextmanager
def project_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = replace(
            config.PATHS, projects=root / "projects", logs=root / "logs",
            models=root / "models", settings_file=root / "settings.json",
        )
        with patch.object(projects, "PATHS", paths), patch.object(config, "PATHS", paths):
            yield root


def sample_transcript():
    doc = new_document(duration=8, model="synthetic")
    doc["segments"] = [
        make_segment(
            0, 1, 3,
            "Email person@example.test; case CASE-12345; call 555-123-4567; "
            "visit 12 Main Street; https://example.test.",
        ),
    ]
    return doc


def test_custom_patterns_reject_unbounded_backtracking():
    safe = deidentification._clean_custom_patterns([
        {"label": "Case", "pattern": r"\bCASE-\d{5}\b"},
    ])
    check("bounded identifier expressions remain supported", len(safe) == 1)
    for pattern in (r"(a+)+$", r"a+b+$", r"(a|aa)+$", r"a{1,}"):
        try:
            deidentification._clean_custom_patterns([
                {"label": "Unsafe", "pattern": pattern},
            ])
        except ValueError:
            rejected = True
        else:
            rejected = False
        check(f"unsafe custom expression is rejected: {pattern}", rejected)


def test_replacements_are_explicit_reversible_and_revision_tracked():
    with project_root() as root:
        project = projects.create_project("Synthetic de-identification")
        slug = project["slug"]
        document = projects.add_document(slug, "synthetic.wav", data=b"audio")
        doc_id = document["id"]
        doc = sample_transcript()
        projects.save_transcript(slug, doc_id, doc)
        defaults = deidentification._find_candidates(
            doc, {name: True for name in deidentification.DEFAULT_FLAGS}, [],
        )
        check("common identifier flags cover contact, address, network, and date forms",
              {"Email address", "Phone number", "Possible street address", "Web address"}
              .issubset({item["label"] for item in defaults}))
        report = deidentification.scan(
            slug, doc_id, doc,
            flags={"email": True, "phone": False, "ssn": False, "date": False, "ip_address": False},
            custom_patterns=[{"label": "Case number", "pattern": r"\bCASE-\d{5}\b"}],
        )
        check("built-in and custom patterns are flagged",
              {item["label"] for item in report["items"]} == {"Email address", "Case number"})
        email = next(item for item in report["items"] if item["category"] == "email")
        doc, replaced = deidentification.replace(slug, doc_id, doc, email["id"])
        projects.save_transcript(slug, doc_id, doc, revision=True)
        check("replacement requires explicit action and uses a visible token",
              "[EMAIL_001]" in doc["segments"][0]["text"]
              and replaced["counts"]["resolved"] == 1)
        sidecar = projects.read_json(
            projects.document_dir(slug, doc_id) / deidentification.STATE_FILE
        )
        check("local map stores the original and replacement",
              sidecar["replacements"][0]["original"] == "person@example.test"
              and sidecar["replacements"][0]["replacement"] == "[EMAIL_001]")
        check("replacement creates a transcript revision",
              len(projects.list_revisions(slug, doc_id)) == 1)
        rescanned = deidentification.scan(slug, doc_id, doc)
        check("resolved findings remain in the report after rescanning",
              rescanned["counts"]["resolved"] == 1
              and rescanned["counts"]["flagged"] == 1)

        archive_path = projects.export_project(slug, root, include_media=False)
        package_path = handoff.create_package(slug, doc_id, root)
        with zipfile.ZipFile(archive_path) as archive:
            contents = set(archive.namelist())
            payload = b"".join(archive.read(name) for name in contents)
            check("project archive excludes the private map sidecar",
                  not any(name.endswith(deidentification.STATE_FILE) for name in contents))
            check("project archive does not serialize the replacement mapping",
                  b'"original": "person@example.test"' not in payload)
        with zipfile.ZipFile(package_path) as archive:
            contents = set(archive.namelist())
            transcript = archive.read("transcript.json")
            check("review package excludes private map and report",
                  not any(name.endswith(deidentification.STATE_FILE) for name in contents)
                  and b"person@example.test" not in transcript
                  and b'"replacements"' not in transcript)

        doc, undone = deidentification.undo(slug, doc_id, doc, email["id"])
        projects.save_transcript(slug, doc_id, doc, revision=True)
        check("undo restores source text and creates another revision",
              "person@example.test" in doc["segments"][0]["text"]
              and len(projects.list_revisions(slug, doc_id)) == 2
              and undone["counts"]["flagged"] == 2)
        case = next(item for item in undone["items"] if item["label"] == "Case number")
        skipped = deidentification.skip(slug, doc_id, case["id"])
        check("skipped findings remain in the local report",
              skipped["counts"]["skipped"] == 1)


def main() -> int:
    test_custom_patterns_reject_unbounded_backtracking()
    test_replacements_are_explicit_reversible_and_revision_tracked()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
