"""Filesystem-backed project store.

There is no database. A project is a directory of plain JSON and media files,
which means a researcher can inspect it, copy it to an encrypted drive, hand it
to a colleague, or recover a transcript with a text editor if this application
ever stops working. Import and export are therefore just a zip archive.

Layout::

    projects/<slug>/
      project.json
      media/<doc_id>__<original name>
      documents/<doc_id>/
        document.json          source facts, status, engine settings, stats
        transcript.json         canonical transcript (see transcript.py)
        preview.m4a             browser-playable audio companion
        waveform.json           cached envelope for the editor's time ribbon
        revisions/<stamp>.json  autosave history
        outputs/                transcript.txt/.md/.docx/.vtt/.srt/.json
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .config import PATHS, app_version
from .transcript import summary as transcript_summary

PROJECT_FILE = "project.json"
DOCUMENT_FILE = "document.json"
TRANSCRIPT_FILE = "transcript.json"
MANIFEST_FILE = "localscribe-manifest.json"
EXPORT_SUFFIX = ".lsproj"

# Editable project metadata. Anything not in here cannot be set by the client.
EDITABLE_FIELDS = (
    "title",
    "description",
    "principal_investigator",
    "irb_protocol",
    "participant_scheme",
    "consent_notes",
    "glossary",
    "tags",
)

_LOCK = threading.RLock()
_SLUG_CLEAN = re.compile(r"[^a-z0-9]+")


class ProjectError(Exception):
    """A project operation failed for a reason worth showing the user."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def slugify(text: str, fallback: str = "project") -> str:
    normalised = unicodedata.normalize("NFKD", str(text or ""))
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_CLEAN.sub("-", ascii_only).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug or fallback)[:60]


def safe_filename(name: str) -> str:
    """Strip any path component and characters Windows refuses."""
    base = Path(str(name or "")).name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return base[:150] or "file"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def project_dir(slug: str) -> Path:
    clean = slugify(slug)
    if not clean:
        raise ProjectError("Invalid project identifier.")
    path = (PATHS.projects / clean).resolve()
    # Refuse anything that escapes the projects root.
    if not str(path).startswith(str(PATHS.projects.resolve())):
        raise ProjectError("Invalid project path.")
    return path


def document_dir(slug: str, doc_id: str) -> Path:
    clean_id = re.sub(r"[^A-Za-z0-9_-]", "", str(doc_id or ""))
    if not clean_id:
        raise ProjectError("Invalid document identifier.")
    return project_dir(slug) / "documents" / clean_id


def media_dir(slug: str) -> Path:
    return project_dir(slug) / "media"


def outputs_dir(slug: str, doc_id: str) -> Path:
    return document_dir(slug, doc_id) / "outputs"


# ---------------------------------------------------------------------------
# Read / write helpers
# ---------------------------------------------------------------------------

def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def new_project_record(title: str, **fields) -> dict:
    record = {
        "slug": slugify(title),
        "title": (title or "Untitled project").strip(),
        "description": "",
        "principal_investigator": "",
        "irb_protocol": "",
        "participant_scheme": "",
        "consent_notes": "",
        "glossary": "",
        "tags": [],
        "created": _now(),
        "updated": _now(),
        "app_version": app_version(),
    }
    for key in EDITABLE_FIELDS:
        if key in fields and fields[key] is not None:
            record[key] = fields[key]
    return record


def create_project(title: str, **fields) -> dict:
    with _LOCK:
        base = slugify(title)
        slug = base
        counter = 2
        while (PATHS.projects / slug).exists():
            slug = f"{base}-{counter}"
            counter += 1

        record = new_project_record(title, **fields)
        record["slug"] = slug

        root = project_dir(slug)
        (root / "media").mkdir(parents=True, exist_ok=True)
        (root / "documents").mkdir(parents=True, exist_ok=True)
        write_json(root / PROJECT_FILE, record)
        return record


def load_project(slug: str) -> dict:
    root = project_dir(slug)
    record = read_json(root / PROJECT_FILE)
    if record is None:
        raise ProjectError(f"Project '{slug}' was not found.")
    record["slug"] = root.name
    return record


def save_project(slug: str, updates: dict) -> dict:
    with _LOCK:
        record = load_project(slug)
        for key in EDITABLE_FIELDS:
            if key not in updates:
                continue
            value = updates[key]
            if key == "tags":
                if isinstance(value, str):
                    value = [t.strip() for t in value.split(",")]
                value = [str(t).strip() for t in (value or []) if str(t).strip()]
            else:
                value = str(value if value is not None else "").strip()
            record[key] = value
        record["updated"] = _now()
        write_json(project_dir(slug) / PROJECT_FILE, record)
        return record


def delete_project(slug: str) -> None:
    with _LOCK:
        root = project_dir(slug)
        if not root.exists():
            raise ProjectError(f"Project '{slug}' was not found.")
        shutil.rmtree(root, ignore_errors=True)


def list_projects() -> list:
    PATHS.projects.mkdir(parents=True, exist_ok=True)
    out = []
    for entry in sorted(PATHS.projects.iterdir()):
        if not entry.is_dir():
            continue
        record = read_json(entry / PROJECT_FILE)
        if record is None:
            continue
        record["slug"] = entry.name
        record["stats"] = project_stats(entry.name)
        out.append(record)
    out.sort(key=lambda r: r.get("updated") or "", reverse=True)
    return out


def project_stats(slug: str) -> dict:
    docs = list_documents(slug)
    statuses: dict = {}
    words = 0
    duration = 0.0
    for doc in docs:
        statuses[doc["status"]] = statuses.get(doc["status"], 0) + 1
        words += int(doc.get("words") or 0)
        duration += float(doc.get("duration") or 0.0)
    return {
        "documents": len(docs),
        "statuses": statuses,
        "words": words,
        "duration": round(duration, 1),
        "pending": sum(
            1 for d in docs if d["status"] in ("uploaded", "failed", "cancelled")
        ),
        "reviewed": sum(1 for d in docs if d.get("human_edited")),
    }


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def new_document_id() -> str:
    return uuid.uuid4().hex[:12]


def add_document(slug: str, filename: str, data: bytes | None = None, source_path: Path | None = None) -> dict:
    """Register an uploaded file. Media is stored verbatim and never modified."""
    with _LOCK:
        load_project(slug)  # existence check
        doc_id = new_document_id()
        clean = safe_filename(filename)

        media = media_dir(slug)
        media.mkdir(parents=True, exist_ok=True)
        stored = media / f"{doc_id}__{clean}"

        if data is not None:
            stored.write_bytes(data)
        elif source_path is not None:
            shutil.copy2(source_path, stored)
        else:
            raise ProjectError("No file content was provided.")

        record = {
            "id": doc_id,
            "filename": clean,
            "media_path": str(stored.relative_to(project_dir(slug))).replace("\\", "/"),
            "size_bytes": stored.stat().st_size,
            "status": "uploaded",
            "status_detail": "Waiting to be transcribed",
            "created": _now(),
            "updated": _now(),
            "duration": None,
            "media": {},
            "engine": {},
            "stats": {},
            "words": 0,
            "segments": 0,
            "human_edited": False,
            "preview": None,
            "error": None,
        }

        target = document_dir(slug, doc_id)
        (target / "outputs").mkdir(parents=True, exist_ok=True)
        (target / "revisions").mkdir(parents=True, exist_ok=True)
        write_json(target / DOCUMENT_FILE, record)
        touch_project(slug)
        return record


def load_document(slug: str, doc_id: str) -> dict:
    record = read_json(document_dir(slug, doc_id) / DOCUMENT_FILE)
    if record is None:
        raise ProjectError(f"Document '{doc_id}' was not found.")
    record["id"] = record.get("id") or doc_id
    return record


def save_document(slug: str, doc_id: str, record: dict) -> dict:
    record["updated"] = _now()
    write_json(document_dir(slug, doc_id) / DOCUMENT_FILE, record)
    return record


def update_document(slug: str, doc_id: str, **changes) -> dict:
    with _LOCK:
        record = load_document(slug, doc_id)
        record.update(changes)
        return save_document(slug, doc_id, record)


def set_status(slug: str, doc_id: str, status: str, detail: str = "", error: str | None = None) -> dict:
    return update_document(
        slug, doc_id, status=status, status_detail=detail, error=error
    )


def list_documents(slug: str) -> list:
    root = project_dir(slug) / "documents"
    if not root.is_dir():
        return []
    out = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        record = read_json(entry / DOCUMENT_FILE)
        if record is None:
            continue
        record["id"] = record.get("id") or entry.name
        record["has_transcript"] = (entry / TRANSCRIPT_FILE).exists()
        out.append(record)
    out.sort(key=lambda r: r.get("created") or "")
    return out


def media_file(slug: str, doc_id: str) -> Path:
    record = load_document(slug, doc_id)
    rel = record.get("media_path") or ""
    path = (project_dir(slug) / rel).resolve()
    if not str(path).startswith(str(project_dir(slug).resolve())):
        raise ProjectError("Invalid media path.")
    if not path.exists():
        raise ProjectError(f"The media file for '{record.get('filename')}' is missing.")
    return path


def delete_document(slug: str, doc_id: str) -> None:
    with _LOCK:
        record = load_document(slug, doc_id)
        rel = record.get("media_path")
        if rel:
            media = project_dir(slug) / rel
            if media.exists():
                media.unlink(missing_ok=True)
        shutil.rmtree(document_dir(slug, doc_id), ignore_errors=True)
        touch_project(slug)


def touch_project(slug: str) -> None:
    root = project_dir(slug)
    record = read_json(root / PROJECT_FILE)
    if record is not None:
        record["updated"] = _now()
        write_json(root / PROJECT_FILE, record)


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

def transcript_path(slug: str, doc_id: str) -> Path:
    return document_dir(slug, doc_id) / TRANSCRIPT_FILE


def load_transcript(slug: str, doc_id: str) -> dict | None:
    return read_json(transcript_path(slug, doc_id))


def save_transcript(slug: str, doc_id: str, doc: dict, revision: bool = True, keep: int = 20) -> dict:
    """Persist the transcript, optionally snapshotting the previous version."""
    with _LOCK:
        path = transcript_path(slug, doc_id)
        if revision and path.exists() and keep > 0:
            previous = read_json(path)
            if previous is not None:
                revisions = document_dir(slug, doc_id) / "revisions"
                revisions.mkdir(parents=True, exist_ok=True)
                write_json(revisions / f"{_stamp()}.json", previous)
                prune_revisions(slug, doc_id, keep)

        write_json(path, doc)

        info = transcript_summary(doc)
        update_document(
            slug, doc_id,
            words=info["words"],
            segments=info["segments"],
            duration=info["duration"] or None,
            human_edited=info["human_edited"],
            retimestamped_at=info["retimestamped_at"],
            low_confidence=info["low_confidence"],
            speakers=info["speakers"],
        )
        touch_project(slug)
        return doc


def prune_revisions(slug: str, doc_id: str, keep: int) -> None:
    revisions = document_dir(slug, doc_id) / "revisions"
    if not revisions.is_dir():
        return
    files = sorted(revisions.glob("*.json"))
    for stale in files[:-keep] if keep > 0 else files:
        stale.unlink(missing_ok=True)


def list_revisions(slug: str, doc_id: str) -> list:
    revisions = document_dir(slug, doc_id) / "revisions"
    if not revisions.is_dir():
        return []
    out = []
    for path in sorted(revisions.glob("*.json"), reverse=True):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        out.append({"id": path.stem, "size_bytes": size})
    return out


def load_revision(slug: str, doc_id: str, revision_id: str) -> dict | None:
    clean = re.sub(r"[^0-9A-Za-z_-]", "", str(revision_id or ""))
    if not clean:
        return None
    return read_json(document_dir(slug, doc_id) / "revisions" / f"{clean}.json")


def list_outputs(slug: str, doc_id: str) -> list:
    out_dir = outputs_dir(slug, doc_id)
    if not out_dir.is_dir():
        return []
    files = []
    for path in sorted(out_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append({
            "name": path.name,
            "format": path.suffix.lstrip("."),
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        })
    return files


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------

def export_project(slug: str, dest_dir: Path | None = None, include_media: bool = True) -> Path:
    """Write a .lsproj archive of an entire project."""
    record = load_project(slug)
    root = project_dir(slug)
    dest_dir = Path(dest_dir or PATHS.logs)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{slug}-{datetime.now().strftime('%Y%m%d-%H%M%S')}{EXPORT_SUFFIX}"

    documents = list_documents(slug)
    manifest = {
        "format": "localscribe-project",
        "format_version": 1,
        "app_version": app_version(),
        "exported": _now(),
        "slug": slug,
        "title": record.get("title"),
        "includes_media": bool(include_media),
        "documents": [
            {"id": d["id"], "filename": d.get("filename"), "status": d.get("status")}
            for d in documents
        ],
    }

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_FILE, json.dumps(manifest, indent=2))
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if not include_media and relative.parts and relative.parts[0] == "media":
                continue
            archive.write(path, str(relative).replace("\\", "/"))

    return dest


def inspect_archive(archive_path: Path) -> dict:
    """Read an archive's manifest without extracting anything."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if MANIFEST_FILE not in names:
                raise ProjectError(
                    "This file is not a Local Scribe project export: its "
                    f"manifest ({MANIFEST_FILE}) is missing."
                )
            manifest = json.loads(archive.read(MANIFEST_FILE).decode("utf-8"))
    except zipfile.BadZipFile as exc:
        raise ProjectError("That file is not a readable archive.") from exc

    if manifest.get("format") != "localscribe-project":
        raise ProjectError("That archive was not produced by Local Scribe.")
    return manifest


def import_project(archive_path: Path, mode: str = "rename") -> dict:
    """Extract a .lsproj archive into projects/.

    ``mode`` is 'rename' (default, keeps both copies) or 'replace'.
    """
    manifest = inspect_archive(archive_path)
    incoming = slugify(manifest.get("slug") or manifest.get("title") or "imported")

    with _LOCK:
        slug = incoming
        if (PATHS.projects / slug).exists():
            if mode == "replace":
                shutil.rmtree(PATHS.projects / slug, ignore_errors=True)
            else:
                counter = 2
                while (PATHS.projects / f"{incoming}-{counter}").exists():
                    counter += 1
                slug = f"{incoming}-{counter}"

        target = project_dir(slug)
        target.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(archive_path) as archive:
            for name in archive.namelist():
                if name == MANIFEST_FILE:
                    continue
                # Refuse absolute paths and traversal, whatever the archive says.
                relative = Path(name.replace("\\", "/"))
                if relative.is_absolute() or ".." in relative.parts:
                    continue
                destination = (target / relative).resolve()
                if not str(destination).startswith(str(target.resolve())):
                    continue
                if name.endswith("/"):
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)

        record = read_json(target / PROJECT_FILE)
        if record is None:
            shutil.rmtree(target, ignore_errors=True)
            raise ProjectError(
                "The archive did not contain a project.json, so it could not "
                "be imported."
            )
        record["slug"] = slug
        if slug != incoming:
            record["title"] = f"{record.get('title') or slug} (imported)"
        record["updated"] = _now()
        write_json(target / PROJECT_FILE, record)

        for directory in ("media", "documents"):
            (target / directory).mkdir(parents=True, exist_ok=True)

        record["stats"] = project_stats(slug)
        record["imported_from"] = Path(archive_path).name
        return record


def reveal(path: Path) -> bool:
    """Open a folder in the platform file manager. Best effort."""
    import subprocess
    import sys

    target = Path(path)
    if not target.exists():
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(target))  # noqa: S606 - documented Windows API
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return True
    except Exception:
        return False
