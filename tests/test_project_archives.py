"""Project archive integrity and compatibility tests.

Run with: python -m tests.test_project_archives
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import config, projects

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


def test_versioned_export_and_restore():
    with project_root() as root:
        project = projects.create_project("Synthetic study", data_sensitivity="may_contain_phi")
        slug = project["slug"]
        doc = projects.add_document(slug, "synthetic.wav", data=b"synthetic audio")
        doc_dir = projects.document_dir(slug, doc["id"])
        projects.write_json(doc_dir / projects.TRANSCRIPT_FILE, {
            "text": "synthetic transcript",
            "engine": {"initial_prompt": "secret-profile-term"},
        })
        projects.update_document(
            slug, doc["id"],
            engine={"name": "synthetic-engine", "initial_prompt": "secret-profile-term"},
            provenance={
                "vocabulary_profile": {
                    "id": "synthetic-profile", "name": "Synthetic preset",
                    "target": "initial_prompt", "settings": {"language": "en"},
                },
                "transcription_settings": {"language": "en"},
            },
        )
        (doc_dir / "waveform.json").write_text("synthetic cache")
        (projects.project_dir(slug) / "logs").mkdir()
        (projects.project_dir(slug) / "logs" / "debug.log").write_text("synthetic log")
        (projects.project_dir(slug) / "cache").mkdir()
        (projects.project_dir(slug) / "cache" / "temporary.bin").write_bytes(b"cache")
        preview = doc_dir / "preview.m4a"
        preview.write_bytes(b"preview")

        archive_path = projects.export_project(slug, root, include_media=False)
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read(projects.MANIFEST_FILE))
            names = set(archive.namelist())
            check("format version", manifest["format_version"] == 2)
            check("project identity and sensitivity",
                  manifest["project_id"] == project["project_id"]
                  and manifest["data_sensitivity"] == "may_contain_phi")
            check("transcription provenance",
                  manifest["transcription_provenance"][0]["engine"]["name"] == "synthetic-engine")
            check("profile and settings provenance is exported without terms",
                  manifest["transcription_provenance"][0]["provenance"]
                  ["vocabulary_profile"]["id"] == "synthetic-profile"
                  and b"secret-profile-term" not in b"".join(
                      archive.read(name) for name in names
                  ))
            check("legacy engine vocab is redacted from archive metadata",
                  manifest["transcription_provenance"][0]["engine"]["initial_prompt"]
                  == "[omitted]")
            check("transcript-only excludes media", not any(name.startswith("media/") for name in names))
            check("transcript-only excludes preview", not any(name.endswith("preview.m4a") for name in names))
            check("transcript remains", any(name.endswith("transcript.json") for name in names))
            check("transient files excluded",
                  not any("waveform.json" in name or "/logs/" in name or "/cache/" in name for name in names))
            entries = {entry["path"]: entry for entry in manifest["files"]}
            check("manifest covers every archive payload",
                  set(entries) == names - {projects.MANIFEST_FILE})
            doc_path = f"documents/{doc['id']}/{projects.TRANSCRIPT_FILE}"
            transcript = archive.read(doc_path)
            check("manifest size and checksum",
                  entries[doc_path]["size_bytes"] == len(transcript)
                  and entries[doc_path]["sha256"] == hashlib.sha256(transcript).hexdigest())

        check("inspect verifies archive", projects.inspect_archive(archive_path)["format_version"] == 2)
        imported = projects.import_project(archive_path)
        check("restore preserves project identity and sensitivity",
              imported["project_id"] == project["project_id"]
              and imported["data_sensitivity"] == "may_contain_phi")
        check("restore reports verified integrity", imported["archive_integrity_verified"])
        check("transcript-only import records missing media",
              imported["archive_includes_media"] is False
              and imported["missing_media_documents"] == [doc["id"]]
              and imported["archive_import"]["integrity_verified"])
        check("document listing identifies missing source media",
              projects.list_documents(imported["slug"])[0]["media_missing"])
        check("restore retains transcript",
              (projects.document_dir(imported["slug"], doc["id"]) / projects.TRANSCRIPT_FILE).exists())


def test_tampering_is_rejected_before_replace():
    with project_root() as root:
        project = projects.create_project("Synthetic protected project")
        slug = project["slug"]
        doc = projects.add_document(slug, "synthetic.wav", data=b"synthetic")
        archive_path = projects.export_project(slug, root)
        tampered_path = root / "tampered.lsproj"
        target_name = f"documents/{doc['id']}/{projects.DOCUMENT_FILE}"
        with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(tampered_path, "w") as target:
            check("media-included archive contains source media",
                  any(name.startswith("media/") for name in source.namelist()))
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == target_name:
                    data += b"tampered"
                target.writestr(info, data)

        original = (projects.project_dir(slug) / projects.PROJECT_FILE).read_bytes()
        try:
            projects.inspect_archive(tampered_path)
        except projects.ProjectError:
            check("tampered archive rejected by inspect", True)
        else:
            check("tampered archive rejected by inspect", False)
        try:
            projects.import_project(tampered_path, mode="replace")
        except projects.ProjectError:
            check("tampered archive rejected by import", True)
        else:
            check("tampered archive rejected by import", False)
        check("failed replace leaves original untouched",
              (projects.project_dir(slug) / projects.PROJECT_FILE).read_bytes() == original)


def test_legacy_archive_and_unsafe_paths():
    with project_root() as root:
        legacy = root / "legacy.lsproj"
        with zipfile.ZipFile(legacy, "w") as archive:
            archive.writestr(projects.MANIFEST_FILE, json.dumps({
                "format": "localscribe-project", "format_version": 1,
                "slug": "legacy-study", "title": "Legacy study",
            }))
            archive.writestr(projects.PROJECT_FILE, json.dumps({
                "slug": "legacy-study", "title": "Legacy study",
            }))
        check("legacy archive remains inspectable",
              projects.inspect_archive(legacy)["format_version"] == 1)
        imported = projects.import_project(legacy)
        check("legacy archive receives conservative defaults",
              imported["data_sensitivity"] == projects.DEFAULT_SENSITIVITY
              and bool(imported["project_id"]))
        check("legacy import reports unavailable checksum assurance",
              not imported["archive_integrity_verified"])

        unsafe = root / "unsafe.lsproj"
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr(projects.MANIFEST_FILE, json.dumps({
                "format": "localscribe-project", "format_version": 1,
                "slug": "unsafe-study",
            }))
            archive.writestr("../outside.txt", "synthetic")
        try:
            projects.import_project(unsafe)
        except projects.ProjectError:
            check("unsafe legacy path rejected", True)
        else:
            check("unsafe legacy path rejected", False)
        check("unsafe archive creates no project",
              not (config.PATHS.projects / "unsafe-study").exists())


def test_golden_version_one_archive():
    fixture = Path(__file__).parent / "fixtures" / "project-v1.lsproj"
    with project_root():
        manifest = projects.inspect_archive(fixture)
        check("golden v1 archive stays inspectable", manifest["format_version"] == 1)
        imported = projects.import_project(fixture)
        documents = projects.list_documents(imported["slug"])
        check("golden v1 import remains unverifiable",
              not imported["archive_integrity_verified"]
              and imported["archive_format_version"] == 1)
        check("golden v1 transcript and missing-media state survive",
              len(documents) == 1
              and documents[0]["has_transcript"]
              and documents[0]["media_missing"]
              and imported["missing_media_documents"] == ["legacy-doc"])


def main() -> int:
    tests = [
        test_versioned_export_and_restore,
        test_tampering_is_rejected_before_replace,
        test_legacy_archive_and_unsafe_paths,
        test_golden_version_one_archive,
    ]
    for test in tests:
        print(f"\n{test.__name__}")
        test()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
