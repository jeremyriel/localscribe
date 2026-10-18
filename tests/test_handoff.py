"""Checks for portable offline review packages, annotations, and conflicts."""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import config, handoff, projects
from app.transcript import make_segment, new_document

FAILURES: list = []


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


def sample_transcript(text="source transcript"):
    doc = new_document(duration=8, model="synthetic")
    doc["segments"] = [make_segment(0, 1, 3, text)]
    return doc


def rewrite_package(
    source: Path, target: Path, *, transcript=None, annotations=None,
    refresh_manifest: bool = True,
):
    with zipfile.ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["manifest.json"])
    if transcript is not None:
        files["transcript.json"] = json.dumps(transcript).encode()
    if annotations is not None:
        files["annotations.json"] = json.dumps(annotations).encode()
        if transcript is not None:
            transcript["annotations"] = annotations
            files["transcript.json"] = json.dumps(transcript).encode()
    if refresh_manifest:
        manifest["files"] = [
            {
                "path": name,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, data in sorted(files.items())
            if name != "manifest.json"
        ]
        files["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)


def test_package_contains_selected_transcript_instructions_and_optional_media():
    with project_root() as root:
        project = projects.create_project("Synthetic handoff")
        doc = projects.add_document(project["slug"], "synthetic.wav", data=b"source-media")
        projects.save_transcript(project["slug"], doc["id"], sample_transcript())
        package_path = handoff.create_package(
            project["slug"], doc["id"], root, include_media=True,
            instructions="Please check the speaker labels.",
        )
        with zipfile.ZipFile(package_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            check("package uses the offline review format",
                  manifest["format"] == handoff.FORMAT and manifest["format_version"] == 1)
            check("package includes selected transcript and instructions",
                  json.loads(archive.read("transcript.json"))["segments"][0]["text"]
                  == "source transcript"
                  and b"speaker labels" in archive.read("instructions.txt"))
            check("optional media is included only when requested",
                  manifest["includes_media"] and archive.read("media/synthetic.wav") == b"source-media")

        transcript_only = handoff.create_package(
            project["slug"], doc["id"], root, include_media=False,
        )
        with zipfile.ZipFile(transcript_only) as archive:
            check("media omission is explicit",
                  not json.loads(archive.read("manifest.json"))["includes_media"]
                  and not any(name.startswith("media/") for name in archive.namelist()))


def test_annotations_import_and_conflict_resolution_preserve_versions():
    with project_root() as root:
        project = projects.create_project("Synthetic conflict")
        slug = project["slug"]
        document = projects.add_document(slug, "synthetic.wav", data=b"source")
        doc_id = document["id"]
        original = sample_transcript()
        projects.save_transcript(slug, doc_id, original)
        package = handoff.create_package(slug, doc_id, root)
        incoming = sample_transcript("reviewer correction")
        annotation = {
            "id": "question-1",
            "kind": "question",
            "text": "Is this name correct?",
            "at": 2.25,
            "segment_id": 0,
            "created_at": "2026-10-12T04:00:00+00:00",
            "author": "Reviewer",
        }
        returned = root / "returned.lsreview"
        rewrite_package(
            package, returned, transcript=incoming, annotations=[annotation],
            refresh_manifest=False,
        )
        projects.save_transcript(slug, doc_id, sample_transcript("local correction"))

        result = handoff.import_package(returned, slug, doc_id)
        active = projects.load_transcript(slug, doc_id)
        check("conflicting import leaves the local transcript active",
              result["conflict"] is not None
              and active["segments"][0]["text"] == "local correction")
        check("timestamped reviewer question persists despite conflict",
              active["annotations"][0]["text"] == annotation["text"]
              and active["annotations"][0]["at"] == 2.25
              and bool(active["annotations"][0]["created_at"]))
        conflicts = handoff.list_conflicts(slug, doc_id)
        conflict_id = result["conflict"]
        check("conflict preserves local and incoming transcript files",
              conflicts[0]["status"] == "unresolved"
              and json.loads(handoff.conflict_file(slug, doc_id, conflict_id, "local").read_text())[
                  "segments"
              ][0]["text"] == "local correction"
              and json.loads(handoff.conflict_file(slug, doc_id, conflict_id, "incoming").read_text())[
                  "segments"
              ][0]["text"] == "reviewer correction")
        resolved = handoff.resolve_conflict(slug, doc_id, conflict_id, "incoming")
        active = projects.load_transcript(slug, doc_id)
        check("choosing incoming preserves both versions and annotations",
              resolved["resolution"] == "incoming"
              and active["segments"][0]["text"] == "reviewer correction"
              and active["annotations"][0]["id"] == "question-1"
              and len(list((projects.document_dir(slug, doc_id) / "conflicts" / conflict_id).glob(
                  "*-transcript.json"
              ))) == 3)


def test_nonconflicting_return_and_annotation_validation():
    with project_root() as root:
        project = projects.create_project("Synthetic no conflict")
        slug = project["slug"]
        document = projects.add_document(slug, "synthetic.wav", data=b"source")
        doc_id = document["id"]
        projects.save_transcript(slug, doc_id, sample_transcript())
        package = handoff.create_package(slug, doc_id, root)
        incoming = sample_transcript("reviewed without local edits")
        returned = root / "returned.lsreview"
        rewrite_package(package, returned, transcript=incoming)
        result = handoff.import_package(returned, slug, doc_id)
        check("non-conflicting review is applied without a conflict",
              result["conflict"] is None
              and projects.load_transcript(slug, doc_id)["segments"][0]["text"]
              == "reviewed without local edits")
        try:
            handoff.clean_annotations([{
                "kind": "comment", "text": "Outside media", "at": 99,
            }], duration=8)
        except handoff.HandoffError:
            check("out-of-range annotations are rejected", True)
        else:
            check("out-of-range annotations are rejected", False)


def test_annotation_only_return_does_not_conflict_with_local_transcript_edit():
    with project_root() as root:
        project = projects.create_project("Synthetic annotation-only handoff")
        slug = project["slug"]
        document = projects.add_document(slug, "synthetic.wav", data=b"source")
        doc_id = document["id"]
        projects.save_transcript(slug, doc_id, sample_transcript())
        package = handoff.create_package(slug, doc_id, root)
        projects.save_transcript(slug, doc_id, sample_transcript("local correction"))
        annotation = {
            "id": "comment-1", "kind": "comment", "text": "Checked this point",
            "at": 2, "created_at": "2026-10-12T04:00:00+00:00",
        }
        returned = root / "annotation-only.lsreview"
        rewrite_package(
            package, returned, annotations=[annotation], refresh_manifest=False,
        )
        result = handoff.import_package(returned, slug, doc_id)
        active = projects.load_transcript(slug, doc_id)
        check("annotation-only return merges without reverting or conflicting",
              result["conflict"] is None
              and active["segments"][0]["text"] == "local correction"
              and active["annotations"][0]["id"] == "comment-1")


def main() -> int:
    for test in (
        test_package_contains_selected_transcript_instructions_and_optional_media,
        test_annotations_import_and_conflict_resolution_preserve_versions,
        test_nonconflicting_return_and_annotation_validation,
        test_annotation_only_return_does_not_conflict_with_local_transcript_edit,
    ):
        print(f"\n{test.__name__}")
        test()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
