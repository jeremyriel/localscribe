"""Synthetic import-to-export regression test with golden deliverables.

Run with: python -m tests.test_workflow_e2e
Set LOCALSCRIBE_UPDATE_GOLDENS=1 only when intentionally refreshing fixtures.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, exporters, projects, proofread, realign, transcript, validation
from app.config import default_settings

FIXTURES = Path(__file__).parent / "fixtures"
GOLDENS = FIXTURES / "workflow-golden"
FIXED_TIME = "2026-01-02T03:04:05+00:00"
FORMATS = ["srt", "vtt", "docx", "json"]
FAILURES: list[str] = []
_write_text = Path.write_text


def _lf_write_text(self, data, encoding=None, errors=None, newline=None):
    # Archive hashes in the golden manifest must not depend on the host's CRLF/LF default.
    return _write_text(self, data, encoding=encoding, errors=errors, newline="\n")


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


@contextmanager
def isolated_project_root():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths = replace(
            config.PATHS,
            projects=root / "projects",
            logs=root / "logs",
            models=root / "models",
            settings_file=root / "settings.json",
        )
        with (
            patch.object(projects, "PATHS", paths),
            patch.object(config, "PATHS", paths),
            patch.object(projects, "_now", return_value=FIXED_TIME),
            patch.object(projects, "new_document_id", return_value="fixture-document"),
            patch.object(transcript, "_now", return_value=FIXED_TIME),
            patch.object(realign, "_now", return_value=FIXED_TIME),
            patch.object(Path, "write_text", _lf_write_text),
        ):
            yield root


def _normalise_docx_package(path: Path) -> None:
    """Fix ZIP and core-property timestamps so the archive golden is repeatable."""
    package_ns = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
    dcterms_ns = "http://purl.org/dc/terms/"
    replacement = path.with_suffix(".normalized.docx")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(replacement, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "docProps/core.xml":
                root = ET.fromstring(content)
                for element in root.iter():
                    if (
                        element.tag.startswith(f"{{{package_ns}}}")
                        or element.tag.startswith(f"{{{dcterms_ns}}}")
                    ) and element.tag.rsplit("}", 1)[-1] in {"created", "modified"}:
                        element.text = FIXED_TIME.replace("+00:00", "Z")
                content = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            normalized = zipfile.ZipInfo(info.filename, (2026, 1, 2, 3, 4, 6))
            normalized.compress_type = info.compress_type
            normalized.create_system = 3  # ZipInfo defaults to 0 on Windows, 3 elsewhere
            normalized.external_attr = info.external_attr
            target.writestr(normalized, content)
    replacement.replace(path)


def _docx_text(path: Path) -> str:
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as package:
        root = ET.fromstring(package.read("word/document.xml"))
    paragraphs = []
    for paragraph in root.iter(f"{namespace}p"):
        parts = []
        for child in paragraph.iter():
            if child.tag == f"{namespace}t":
                parts.append(child.text or "")
            elif child.tag == f"{namespace}tab":
                parts.append("\t")
        if parts:
            paragraphs.append("".join(parts))
    return "\n".join(paragraphs) + "\n"


def _build_doc():
    source = json.loads((FIXTURES / "transcription-fixture.json").read_text(encoding="utf-8"))
    doc = transcript.new_document(
        duration=source["duration"],
        model="synthetic-fixture",
        language=source["language"],
        language_probability=source["language_probability"],
        engine={"name": "synthetic-fixture"},
    )
    doc["created"] = FIXED_TIME
    doc["updated"] = FIXED_TIME
    doc["speakers"] = [
        {"id": "s1", "name": "Entrevistadora", "short": "E", "color": "#0f766e"},
        {"id": "s2", "name": "Participante 01", "short": "P01", "color": "#9a5b00"},
    ]
    doc["segments"] = [
        transcript.make_segment(
            index,
            segment["start"],
            segment["end"],
            segment["text"],
            [
                transcript.make_word(
                    word["text"], word["start"], word["end"], word["probability"]
                )
                for word in segment["words"]
            ],
        )
        for index, segment in enumerate(source["segments"])
    ]
    transcript.normalise(doc)
    transcript.assign_speaker(doc, [0], "s1")
    transcript.assign_speaker(doc, [1], "s2")
    transcript.apply_edits(doc, [{"id": 1, "text": "Muy bien, muchas gracias."}])
    realign.retimestamp(doc)
    validation.transition(doc, "start", reviewer="Revisor", at=FIXED_TIME)
    validation.transition(
        doc, "validate", reviewer="Revisor", notes="Revisado", at=FIXED_TIME
    )
    return doc


def _compare_or_update(name: str, content: str) -> None:
    path = GOLDENS / name
    if os.environ.get("LOCALSCRIBE_UPDATE_GOLDENS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        check(f"updated golden {name}", True)
        return
    expected = path.read_text(encoding="utf-8") if path.exists() else None
    check(f"golden {name} matches", content == expected,
          "refresh intentionally with LOCALSCRIBE_UPDATE_GOLDENS=1" if expected is None or content != expected else "")


def test_synthetic_full_workflow():
    with isolated_project_root() as root:
        seed = projects.create_project(
            "Synthetic regression study",
            data_sensitivity="no_phi",
        )
        seed_record = projects.load_project(seed["slug"])
        seed_record["project_id"] = "synthetic-project-id"
        projects.write_json(
            projects.project_dir(seed["slug"]) / projects.PROJECT_FILE, seed_record
        )
        seed_archive = projects.export_project(seed["slug"], root / "seed")
        imported_seed = projects.import_project(seed_archive)
        slug = imported_seed["slug"]
        check("a project archive imports before processing",
              imported_seed["archive_integrity_verified"])

        record = projects.add_document(
            slug, "synthetic-interview.wav", data=b"synthetic audio fixture"
        )
        doc = _build_doc()
        projects.update_document(
            slug, record["id"], status="transcribed", duration=doc["duration"],
            engine={"name": "synthetic-fixture"}, stats={"segments": 2},
        )
        projects.save_transcript(slug, record["id"], doc, revision=False)
        saved = projects.load_transcript(slug, record["id"])
        check("synthetic processing fixture creates a transcript",
              saved["language"] == "es" and len(saved["segments"]) == 2)
        check("manual speaker assignments and reviewed edit survive",
              saved["segments"][0]["speaker"] == "Entrevistadora"
              and saved["segments"][1]["speaker"] == "Participante 01"
              and saved["segments"][1]["text"] == "Muy bien, muchas gracias."
              and saved["validation"]["status"] == "validated")
        check("language is preserved while English-only proofreading stays disabled",
              not proofread.supports_language(saved["language"]))

        output_dir = projects.document_dir(slug, record["id"]) / "outputs"
        provenance = validation.provenance(saved, FIXED_TIME, FORMATS)
        validation.record_export(saved, FIXED_TIME, FORMATS)
        metadata = {
            "title": "Synthetic interview",
            "filename": "synthetic-interview.wav",
            "project": "Synthetic regression study (imported)",
            "export_provenance": provenance,
        }
        written = exporters.export_all(
            saved, output_dir, default_settings(), metadata, formats=FORMATS,
            basename="synthetic-interview",
        )
        check("all golden export formats are written",
              set(written) == set(FORMATS)
              and all(not value.startswith("error:") for value in written.values()),
              str(written))
        if "docx" in written and not written["docx"].startswith("error:"):
            _normalise_docx_package(Path(written["docx"]))
        projects.save_transcript(slug, record["id"], saved, revision=False)

        if all(fmt in written and Path(written[fmt]).is_file() for fmt in FORMATS):
            _compare_or_update("interview.srt", Path(written["srt"]).read_text(encoding="utf-8"))
            _compare_or_update("interview.vtt", Path(written["vtt"]).read_text(encoding="utf-8"))
            _compare_or_update("interview.json", Path(written["json"]).read_text(encoding="utf-8"))
            _compare_or_update("interview.docx.txt", _docx_text(Path(written["docx"])))

        archive_path = projects.export_project(slug, root / "archives")
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read(projects.MANIFEST_FILE))
        _compare_or_update(
            "archive-manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        restored = projects.import_project(archive_path)
        restored_doc = projects.load_transcript(restored["slug"], record["id"])
        check("round-trip archive import verifies and preserves the review",
              restored["archive_integrity_verified"]
              and restored_doc["segments"][1]["text"] == "Muy bien, muchas gracias."
              and restored_doc["validation"]["status"] == "validated")
        check("Box regressions use the existing synthetic mocked suite",
              (Path(__file__).parent / "test_box_sync.py").is_file())


def test_packaged_data_roots_are_external():
    packaging = ROOT / "packaging"
    windows = (packaging / "windows" / "build.ps1").read_text(encoding="utf-8")
    macos = (packaging / "macos" / "build.sh").read_text(encoding="utf-8")
    linux = (packaging / "linux" / "build-appimage.sh").read_text(encoding="utf-8")
    check("Windows keeps mutable data in the per-user Local AppData folder",
          r"LOCALSCRIBE_DATA_DIR=%LOCALAPPDATA%\Local Scribe" in windows)
    check("macOS keeps mutable data in Application Support",
          'LOCALSCRIBE_DATA_DIR="$HOME/Library/Application Support/Local Scribe"' in macos)
    check("Linux keeps mutable data in the XDG user data directory",
          'LOCALSCRIBE_DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/localscribe"' in linux)


def main() -> int:
    for test in (test_synthetic_full_workflow, test_packaged_data_roots_are_external):
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
