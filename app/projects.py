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

import hashlib
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
from uuid import uuid4

from .config import PATHS, SETTINGS, app_version
from . import languages
from .sensitivity import DEFAULT_SENSITIVITY, future_backup_gate, normalize_sensitivity, validate_sensitivity
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
    "vocabulary_profile_id",
    "tags",
    "data_sensitivity",
    "transcription_language",
    "operation",
)

_LOCK = threading.RLock()
_SLUG_CLEAN = re.compile(r"[^a-z0-9]+")


class ProjectError(Exception):
    """A project operation failed for a reason worth showing the user."""


class ProjectValidationError(ProjectError):
    """Incoming project metadata is invalid."""


class ProjectConflictError(ProjectError):
    """An operation needs an explicit choice because a project already exists."""


class ProjectCancelled(ProjectError):
    """An archive operation was cancelled by the user."""


def _validate_sensitivity(value) -> str:
    try:
        return validate_sensitivity(value)
    except ValueError as exc:
        raise ProjectValidationError(str(exc)) from exc


def _normalize_project(record: dict) -> dict:
    record["data_sensitivity"] = normalize_sensitivity(record.get("data_sensitivity"))
    return record


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

def project_operation(project: dict) -> str:
    """The operation a project is currently set to. Projects with none saved
    (made before the choice existed) are Transcribe."""
    return languages.parse_operation(project.get("operation"), languages.TRANSCRIBE) or languages.TRANSCRIBE


def clean_language(value) -> str | None:
    """A project's language choice: \"\" (none), \"auto\", a code, or None if invalid."""
    text = str(value if value is not None else "").strip()
    return languages.parse(text) if text else ""


def new_project_record(title: str, **fields) -> dict:
    record = {
        "project_id": uuid4().hex,
        "slug": slugify(title),
        "title": (title or "Untitled project").strip(),
        "description": "",
        "principal_investigator": "",
        "irb_protocol": "",
        "participant_scheme": "",
        "consent_notes": "",
        "glossary": "",
        "vocabulary_profile_id": "",
        "transcription_language": "",   # "" = use the Settings default
        "operation": "",                 # "" = transcribe (projects made before this existed)
        "tags": [],
        "data_sensitivity": DEFAULT_SENSITIVITY,
        "created": _now(),
        "updated": _now(),
        "app_version": app_version(),
    }
    for key in EDITABLE_FIELDS:
        if key == "data_sensitivity" and key in fields:
            record[key] = _validate_sensitivity(fields[key])
            continue
        if key in fields and fields[key] is not None:
            record[key] = fields[key]
    record["transcription_language"] = clean_language(record["transcription_language"]) or ""
    record["operation"] = languages.parse_operation(record["operation"]) or ""
    return record


def create_project(title: str, **fields) -> dict:
    # A new project starts from the Settings default operation, saved
    # explicitly. Projects made earlier have no saved operation and stay on
    # Transcribe whatever that default later becomes.
    if fields.get("operation") in (None, ""):
        fields["operation"] = languages.parse_operation(SETTINGS.get("task"), languages.TRANSCRIBE)
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
    return _normalize_project(record)


def save_project(slug: str, updates: dict) -> dict:
    with _LOCK:
        record = load_project(slug)
        for key in EDITABLE_FIELDS:
            if key not in updates:
                continue
            value = updates[key]
            if key == "data_sensitivity":
                value = _validate_sensitivity(value)
            elif key == "tags":
                if isinstance(value, str):
                    value = [t.strip() for t in value.split(",")]
                value = [str(t).strip() for t in (value or []) if str(t).strip()]
            elif key == "vocabulary_profile_id":
                value = str(value if value is not None else "").strip()
            elif key == "operation":
                value = languages.parse_operation(value)
                if value is None:
                    continue      # an unrecognised operation is ignored
            elif key == "transcription_language":
                # "" clears the project's choice; anything unrecognised is
                # ignored rather than stored, so a bad value cannot reach the
                # engine later.
                value = clean_language(value)
                if value is None:
                    continue
            else:
                value = str(value if value is not None else "").strip()
            record[key] = value
        record["updated"] = _now()
        write_json(project_dir(slug) / PROJECT_FILE, record)
        return record


# ---------------------------------------------------------------------------
# Per-project backup record (machine-local; never exported or imported)
# ---------------------------------------------------------------------------

BACKUP_FILE = "backup.local.json"
BACKUP_RECORD_VERSION = 1
BACKUP_HISTORY_LIMIT = 50
_BACKUP_SELECTIONS = {"manual", "picker", "legacy_global"}
_BACKUP_EVENT_STATUSES = {"complete", "failed", "cancelled"}


def is_machine_local(name: str) -> bool:
    """Files named *.local.json describe this computer and never travel in archives."""
    return str(name).casefold().endswith(".local.json")


def _empty_backup_record() -> dict:
    return {
        "version": BACKUP_RECORD_VERSION,
        "destination": "",
        "selection": "",
        "selected_at": "",
        "attestation": None,
        "history": [],
    }


def _clean_backup_event(event) -> dict | None:
    if not isinstance(event, dict) or event.get("status") not in _BACKUP_EVENT_STATUSES:
        return None
    cleaned = {
        "finished_at": str(event.get("finished_at") or ""),
        "status": event["status"],
        "filename": str(event.get("filename") or ""),
        "total_bytes": int(event["total_bytes"]) if isinstance(event.get("total_bytes"), int) else 0,
        "local_copy_verified": event.get("local_copy_verified") is True,
        "destination_name": str(event.get("destination_name") or ""),
    }
    if event.get("error"):
        cleaned["error"] = str(event["error"])[:300]
    return cleaned


def _read_backup_file(path: Path) -> dict:
    raw = read_json(path, None)
    record = _empty_backup_record()
    if not isinstance(raw, dict) or raw.get("version") != BACKUP_RECORD_VERSION:
        return record
    destination = raw.get("destination")
    if isinstance(destination, str):
        record["destination"] = destination
        record["selection"] = raw.get("selection") if raw.get("selection") in _BACKUP_SELECTIONS else ""
        record["selected_at"] = str(raw.get("selected_at") or "")
    attestation = raw.get("attestation")
    if isinstance(attestation, dict) and record["destination"]:
        record["attestation"] = {
            "confirmed_at": str(attestation.get("confirmed_at") or ""),
            "destination_root": str(attestation.get("destination_root") or ""),
        }
    history = raw.get("history")
    if isinstance(history, list):
        record["history"] = [
            event for event in map(_clean_backup_event, history) if event is not None
        ][-BACKUP_HISTORY_LIMIT:]
    return record


def load_backup_record(slug: str) -> dict:
    """The project's local backup record, or an empty one if absent or unreadable."""
    return _read_backup_file(project_dir(slug) / BACKUP_FILE)


def _write_backup_record(slug: str, record: dict) -> dict:
    write_json(project_dir(slug) / BACKUP_FILE, record)
    return record


def set_backup_destination(slug: str, destination: Path, selection: str = "manual") -> dict:
    """Save this project's backup folder; a changed folder drops any earlier attestation."""
    if selection not in _BACKUP_SELECTIONS:
        raise ProjectValidationError("Unknown destination selection source.")
    destination = Path(destination)
    if not destination.is_absolute():
        raise ProjectValidationError("The backup folder must be an absolute path.")
    with _LOCK:
        load_project(slug)
        record = load_backup_record(slug)
        if record["destination"] != str(destination):
            record["attestation"] = None
        record["destination"] = str(destination)
        record["selection"] = selection
        record["selected_at"] = _now()
        return _write_backup_record(slug, record)


def clear_backup_destination(slug: str) -> dict:
    """Forget this project's backup folder and attestation; history is kept."""
    with _LOCK:
        load_project(slug)
        record = load_backup_record(slug)
        record["destination"] = ""
        record["selection"] = ""
        record["selected_at"] = ""
        record["attestation"] = None
        return _write_backup_record(slug, record)


def record_backup_attestation(
    slug: str, destination_root: Path, *, independently_verified_box_health: bool = False
) -> dict:
    """Record only the latest explicit attestation, not a reusable permission."""
    with _LOCK:
        project = load_project(slug)
        destination_root = Path(destination_root)
        result = future_backup_gate(
            project["data_sensitivity"], destination_root.name,
            independently_verified_box_health=independently_verified_box_health,
        )
        if not result.override_used or not destination_root.is_absolute():
            raise ProjectValidationError(
                "An override requires explicit independent Box Health verification "
                "for a PHI/unknown project and an absolute destination folder path."
            )
        record = load_backup_record(slug)
        record["attestation"] = {
            "confirmed_at": _now(),
            "destination_root": str(destination_root),
        }
        if not record["destination"]:
            record["destination"] = str(destination_root)
        return _write_backup_record(slug, record)


def any_backup_destination() -> bool:
    """Whether any project on this computer has a backup folder (a yes/no only)."""
    try:
        entries = [entry for entry in PATHS.projects.iterdir() if entry.is_dir()]
    except OSError:
        return False
    return any(_read_backup_file(entry / BACKUP_FILE)["destination"] for entry in entries)


def append_backup_event(slug: str, event: dict) -> dict | None:
    """Add one finished backup attempt to this project's local history."""
    cleaned = _clean_backup_event(event)
    if cleaned is None:
        return None
    with _LOCK:
        try:
            load_project(slug)
            record = load_backup_record(slug)
            cleaned["finished_at"] = cleaned["finished_at"] or _now()
            record["history"] = (record["history"] + [cleaned])[-BACKUP_HISTORY_LIMIT:]
            _write_backup_record(slug, record)
        except (ProjectError, OSError):
            return None
        return cleaned


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
        _normalize_project(record)
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


def _redact_export_vocabulary(value):
    """Remove prompt and hotword contents from archived provenance JSON."""
    if isinstance(value, dict):
        return {
            key: (
                "[omitted]" if key in {"initial_prompt", "hotwords"}
                else _redact_export_vocabulary(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_export_vocabulary(item) for item in value]
    return value


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
            "status_detail": "Waiting to be processed",
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
    changes: dict = {"status": status, "status_detail": detail, "error": error}
    if status not in ("queued", "probing", "decoding", "transcribing"):
        changes["pending_operation"] = None   # nothing is running for this file
    return update_document(slug, doc_id, **changes)


def list_documents(slug: str) -> list:
    root = project_dir(slug) / "documents"
    if not root.is_dir():
        return []
    project_root = project_dir(slug).resolve()
    out = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        record = read_json(entry / DOCUMENT_FILE)
        if record is None:
            continue
        record["id"] = record.get("id") or entry.name
        record["has_transcript"] = (entry / TRANSCRIPT_FILE).exists()
        media_rel = record.get("media_path")
        if media_rel:
            try:
                media_path = (project_root / _safe_archive_path(media_rel)).resolve()
                media_path.relative_to(project_root)
                record["media_missing"] = not media_path.is_file()
            except (ProjectError, TypeError, ValueError):
                record["media_missing"] = True
        if record["has_transcript"] and "output_status" not in record:
            # Records written before the operation was tracked: fill in what
            # the transcript itself shows, once, so the list can label it
            # truthfully. Purely additive; nothing already stored is changed.
            doc = read_json(entry / TRANSCRIPT_FILE)
            if doc is not None:
                fields = result_fields(transcript_summary(doc))
                record.update(fields)
                update_document(slug, entry.name, **fields)
        out.append(record)
    out.sort(key=lambda r: r.get("created") or "")
    return out


def result_fields(info: dict) -> dict:
    """The parts of a transcript's summary kept on its document record."""
    return {
        "language": info["language"],
        "language_name": info["language_name"],
        "language_requested": info["language_requested"],
        "output_language": info["output_language"],
        "operation": info["operation"],
        "output_status": info["output_status"],
        "length_unit": "characters" if info["spaceless"] else "words",
    }


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


def _snapshot_dir(slug: str) -> Path:
    clean = slugify(slug)
    if not clean:
        raise ProjectError("Invalid project identifier.")
    return PATHS.projects / ".snapshots" / clean


def create_snapshot(
    slug: str, *, include_media: bool = True
) -> dict:
    """Save a verified project archive outside the project directory."""
    record = load_project(slug)
    slug = record["slug"]
    snapshot_dir = _snapshot_dir(slug)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    staging = snapshot_dir / f".tmp-{uuid4().hex}"
    staging.mkdir()
    try:
        with _LOCK:
            archive = export_project(slug, staging, include_media=include_media)
            snapshot_id = f"{slug}-{_stamp()}-{uuid4().hex[:8]}{EXPORT_SUFFIX}"
            target = snapshot_dir / snapshot_id
            archive.replace(target)
        return {
            "snapshot_id": snapshot_id,
            "created": _now(),
            "size_bytes": target.stat().st_size,
            "includes_media": bool(include_media),
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def snapshot_media_state(path: Path) -> str:
    """Whether an archive carries source media, from its own manifest.

    "included" and "omitted" come from the manifest's includes_media flag and its
    file list; "none" means the project had no recordings; "unknown" is a
    version 1 or unreadable archive that cannot say.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read(MANIFEST_FILE).decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return "unknown"
    if not isinstance(manifest, dict) or not isinstance(manifest.get("includes_media"), bool):
        return "unknown"
    if not manifest["includes_media"]:
        return "omitted"
    entries = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    has_media = any(
        isinstance(entry, dict) and str(entry.get("path", "")).startswith("media/")
        for entry in entries
    )
    return "included" if has_media else "none"


def list_snapshots(slug: str) -> list[dict]:
    snapshot_dir = _snapshot_dir(slug)
    if not snapshot_dir.is_dir():
        return []
    snapshots = []
    for path in snapshot_dir.iterdir():
        if path.suffix != EXPORT_SUFFIX or not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshots.append({
            "snapshot_id": path.name,
            "created": datetime.fromtimestamp(stat.st_mtime, timezone.utc).astimezone().isoformat(timespec="seconds"),
            "size_bytes": stat.st_size,
            "media": snapshot_media_state(path),
        })
    return sorted(snapshots, key=lambda item: item["created"], reverse=True)


def list_all_snapshots() -> list[dict]:
    root = PATHS.projects / ".snapshots"
    if not root.is_dir():
        return []
    snapshots = []
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        for snapshot in list_snapshots(project_dir.name):
            snapshot["project_slug"] = project_dir.name
            snapshots.append(snapshot)
    return sorted(snapshots, key=lambda item: item["created"], reverse=True)


def restore_snapshot(slug: str, snapshot_id: str, mode: str = "abort") -> dict | None:
    """Restore a saved archive after validating its identity and integrity."""
    snapshot_dir = _snapshot_dir(slug).resolve()
    candidate = Path(str(snapshot_id))
    if (
        candidate.name != str(snapshot_id)
        or candidate.suffix != EXPORT_SUFFIX
        or candidate.name in ("", ".", "..")
    ):
        raise ProjectValidationError("Invalid snapshot identifier.")
    archive = (snapshot_dir / candidate.name).resolve()
    if archive.parent != snapshot_dir or not archive.is_file():
        raise ProjectError("That recovery snapshot was not found.")
    if mode not in ("rename", "replace", "abort"):
        raise ProjectValidationError("Restore mode must be 'rename', 'replace', or 'abort'.")
    if mode == "abort":
        return None

    manifest = inspect_archive(archive)
    incoming_slug = slugify(manifest.get("slug") or manifest.get("title") or "imported")
    target = project_dir(incoming_slug)
    if mode == "replace" and target.exists():
        create_snapshot(incoming_slug)
    return import_project(archive, mode=mode)


def touch_project(slug: str) -> None:
    root = project_dir(slug)
    record = read_json(root / PROJECT_FILE)
    if record is not None:
        _normalize_project(record)
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
            **result_fields(info),
        )
        touch_project(slug)
        return doc


KEEP_PREFIX = "keep-"


def archive_transcript(slug: str, doc_id: str) -> str | None:
    """Keep the current transcript before a new result replaces it.

    Unlike ordinary autosave revisions these are never pruned: a transcript
    that took an hour to produce and review must survive being replaced by a
    translation (or a redo). They appear in Edit history and can be restored.
    Returns the revision id, or None when there was nothing to keep.
    """
    with _LOCK:
        path = transcript_path(slug, doc_id)
        previous = read_json(path) if path.exists() else None
        if previous is None:
            return None
        operation = languages.operation_of(previous)
        revisions = document_dir(slug, doc_id) / "revisions"
        revisions.mkdir(parents=True, exist_ok=True)
        revision_id = f"{KEEP_PREFIX}{_stamp()}-{operation}"
        write_json(revisions / f"{revision_id}.json", previous)
        return revision_id


def prune_revisions(slug: str, doc_id: str, keep: int) -> None:
    revisions = document_dir(slug, doc_id) / "revisions"
    if not revisions.is_dir():
        return
    files = sorted(f for f in revisions.glob("*.json") if not f.name.startswith(KEEP_PREFIX))
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
        out.append({
            "id": path.stem,
            "size_bytes": size,
            # Earlier results kept when a transcript was replaced, named
            # after the operation that produced them.
            "kept": path.stem.startswith(KEEP_PREFIX),
            "operation": path.stem.rsplit("-", 1)[-1] if path.stem.startswith(KEEP_PREFIX) else "",
        })
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

def zip_info_for(path: Path, archive_name: str, compress_type: int) -> zipfile.ZipInfo:
    """A ZipInfo whose stored name is exactly archive_name.

    ZipInfo.from_file normalises names with os.sep, leaving backslashes in
    orig_filename on Windows; the archive then fails its own read-back.
    """
    info = zipfile.ZipInfo.from_file(path, archive_name)
    info.filename = archive_name
    info.orig_filename = archive_name
    info.compress_type = compress_type
    return info


def write_zip_file(archive: zipfile.ZipFile, path: Path, archive_name: str) -> None:
    info = zip_info_for(path, archive_name, archive.compression)
    with path.open("rb") as source, archive.open(info, "w") as target:
        shutil.copyfileobj(source, target, 1024 * 1024)


def export_project(
    slug: str,
    dest_dir: Path | None = None,
    include_media: bool = True,
    cancel_event=None,
) -> Path:
    """Write a versioned, checksummed .lsproj archive of a project."""

    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ProjectCancelled("Backup cancelled.")

    record = load_project(slug)
    slug = record["slug"]
    root = project_dir(slug)
    dest_dir = Path(dest_dir or PATHS.logs).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = (dest_dir / f"{slug}-{datetime.now().strftime('%Y%m%d-%H%M%S')}{EXPORT_SUFFIX}").resolve()
    if dest.parent != dest_dir:
        raise ProjectError("The export destination must be a direct child of its output folder.")

    documents = list_documents(slug)
    project_id = record.get("project_id") or record.get("slug") or slug
    manifest = {
        "format": "localscribe-project",
        "format_version": 2,
        "app_version": app_version(),
        "exported": _now(),
        "slug": slug,
        "project_id": project_id,
        "title": record.get("title"),
        "includes_media": bool(include_media),
        "data_sensitivity": record.get("data_sensitivity", DEFAULT_SENSITIVITY),
        "documents": [
            {"id": d["id"], "filename": d.get("filename"), "status": d.get("status")}
            for d in documents
        ],
        "transcription_provenance": [
            {
                "document_id": d["id"],
                "engine": _redact_export_vocabulary(d.get("engine", {})),
                "provenance": _redact_export_vocabulary(d.get("provenance", {})),
                "media": d.get("media", {}),
                "stats": d.get("stats", {}),
            }
            for d in documents
        ],
    }

    paths = []
    transient_directories = {"cache", "caches", "logs", "__pycache__"}
    for path in sorted(root.rglob("*")):
        check_cancelled()
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        archive_name = relative.as_posix()
        if archive_name == MANIFEST_FILE:
            continue
        if is_machine_local(path.name):
            continue
        if any(part.casefold() in transient_directories for part in relative.parts):
            continue
        if path.name.casefold() == "waveform.json" or path.name.casefold().endswith(".tmp"):
            continue
        if not include_media and (
            (relative.parts and relative.parts[0] == "media")
            or (len(relative.parts) >= 3 and relative.parts[0] == "documents"
                and relative.parts[-1].startswith("preview."))
        ):
            continue
        paths.append((path, archive_name))

    project_json = dict(record)
    # A destination attestation is about this computer, not the project.
    project_json.pop("box_health_override", None)
    project_json["project_id"] = project_id
    project_json["data_sensitivity"] = record.get(
        "data_sensitivity", DEFAULT_SENSITIVITY
    )
    created = False
    try:
        with dest.open("x+b") as output:
            created = True
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for path, archive_name in paths:
                    check_cancelled()
                    if archive_name == PROJECT_FILE:
                        archive.writestr(
                            archive_name,
                            json.dumps(project_json, indent=2, ensure_ascii=False),
                        )
                    elif path.suffix.casefold() == ".json":
                        try:
                            payload = json.loads(path.read_text(encoding="utf-8"))
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                            payload = None
                        if payload is not None:
                            archive.writestr(
                                archive_name,
                                json.dumps(
                                    _redact_export_vocabulary(payload),
                                    indent=2, ensure_ascii=False,
                                ),
                            )
                        else:
                            info = zip_info_for(path, archive_name, archive.compression)
                            with path.open("rb") as source, archive.open(info, "w") as target:
                                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                    check_cancelled()
                                    target.write(chunk)
                    else:
                        info = zip_info_for(path, archive_name, archive.compression)
                        with path.open("rb") as source, archive.open(info, "w") as target:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                check_cancelled()
                                target.write(chunk)

                for info in archive.infolist():
                    check_cancelled()
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(info) as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            check_cancelled()
                            size += len(chunk)
                            digest.update(chunk)
                    manifest.setdefault("files", []).append({
                        "path": info.filename,
                        "size_bytes": size,
                        "sha256": digest.hexdigest(),
                    })
                archive.writestr(MANIFEST_FILE, json.dumps(manifest, indent=2))
    except Exception:
        if created:
            dest.unlink(missing_ok=True)
        raise

    return dest


def _safe_archive_path(name: str) -> Path:
    normalized = str(name).replace("\\", "/")
    parts = normalized.rstrip("/").split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ProjectError("The project archive contains an unsafe file path.")
    return Path(*parts)


def _validate_archive(archive: zipfile.ZipFile) -> dict:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ProjectError("The project archive contains duplicate file paths.")
    if MANIFEST_FILE not in names:
        raise ProjectError(
            "This file is not a Local Scribe project export: its "
            f"manifest ({MANIFEST_FILE}) is missing."
        )
    for name in names:
        if name != MANIFEST_FILE:
            _safe_archive_path(name)

    try:
        manifest = json.loads(archive.read(MANIFEST_FILE).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectError("The project archive manifest is not valid JSON.") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != "localscribe-project":
        raise ProjectError("That archive was not produced by Local Scribe.")

    version = manifest.get("format_version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ProjectError("This project archive uses an unsupported format version.")
    if version == 1:
        manifest["integrity_verified"] = False
        return manifest
    if version != 2:
        raise ProjectError("This project archive uses an unsupported format version.")
    if (
        not isinstance(manifest.get("includes_media"), bool)
        or not isinstance(manifest.get("documents"), list)
        or not isinstance(manifest.get("transcription_provenance"), list)
    ):
        raise ProjectError("The project archive manifest is missing required version 2 fields.")

    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ProjectError("The project archive manifest has no valid file list.")
    expected = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ProjectError("The project archive manifest has an invalid file entry.")
        name = entry.get("path")
        path = _safe_archive_path(name) if isinstance(name, str) else None
        size = entry.get("size_bytes")
        digest = entry.get("sha256")
        if (
            path is None
            or name == MANIFEST_FILE
            or name in expected
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ProjectError("The project archive manifest has an invalid file entry.")
        expected[name] = (size, digest)

    actual = {name for name in names if name != MANIFEST_FILE and not name.endswith("/")}
    if actual != set(expected):
        raise ProjectError("The project archive file list does not match its manifest.")

    for name, (expected_size, expected_digest) in expected.items():
        digest = hashlib.sha256()
        size = 0
        with archive.open(name) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        if size != expected_size or digest.hexdigest() != expected_digest:
            raise ProjectError(f"The project archive failed its integrity check: {name}.")
    manifest["integrity_verified"] = True
    return manifest


def inspect_archive(archive_path: Path) -> dict:
    """Read an archive's manifest without extracting anything."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            manifest = _validate_archive(archive)
    except zipfile.BadZipFile as exc:
        raise ProjectError("That file is not a readable archive.") from exc
    return manifest


def import_project(archive_path: Path, mode: str = "rename") -> dict:
    """Extract a .lsproj archive into projects/.

    ``mode`` is 'rename' (default, keeps both copies) or 'replace'.
    """
    if mode not in ("rename", "replace", "abort"):
        raise ProjectValidationError("Import mode must be 'rename', 'replace', or 'abort'.")

    PATHS.projects.mkdir(parents=True, exist_ok=True)
    stage = PATHS.projects / f".import-{uuid4().hex}"
    backup = None
    try:
        with zipfile.ZipFile(archive_path) as archive:
            manifest = _validate_archive(archive)
            incoming = slugify(manifest.get("slug") or manifest.get("title") or "imported")
            with _LOCK:
                slug = incoming
                existing = (PATHS.projects / slug).exists()
                if existing and mode == "abort":
                    raise ProjectConflictError(
                        "A project with this name already exists; choose rename or replace."
                    )
                if existing and mode == "rename":
                    counter = 2
                    while (PATHS.projects / f"{incoming}-{counter}").exists():
                        counter += 1
                    slug = f"{incoming}-{counter}"
                target = project_dir(slug)
                stage.mkdir()

                expected = {
                    entry["path"]: (entry["size_bytes"], entry["sha256"])
                    for entry in manifest.get("files", [])
                }
                for info in archive.infolist():
                    name = info.filename
                    if name == MANIFEST_FILE:
                        continue
                    relative = _safe_archive_path(name)
                    if is_machine_local(relative.name):
                        continue
                    destination = stage / relative
                    if info.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(info) as src, destination.open("wb") as dst:
                        for chunk in iter(lambda: src.read(1024 * 1024), b""):
                            size += len(chunk)
                            digest.update(chunk)
                            dst.write(chunk)
                    if name in expected and (
                        size != expected[name][0] or digest.hexdigest() != expected[name][1]
                    ):
                        raise ProjectError(
                            f"The project archive failed its integrity check: {name}."
                        )

                record = read_json(stage / PROJECT_FILE)
                if record is None:
                    raise ProjectError(
                        "The archive did not contain a project.json, so it could not "
                        "be imported."
                    )
                record["slug"] = slug
                # Archives from earlier versions carried this machine-specific record.
                record.pop("box_health_override", None)
                record["project_id"] = (
                    manifest.get("project_id") or record.get("project_id") or uuid4().hex
                )
                record["data_sensitivity"] = manifest.get(
                    "data_sensitivity", record.get("data_sensitivity")
                )
                _normalize_project(record)
                if slug != incoming:
                    record["title"] = f"{record.get('title') or slug} (imported)"
                record["updated"] = _now()
                missing_media_documents = []
                documents_root = stage / "documents"
                if documents_root.is_dir():
                    for directory in documents_root.iterdir():
                        if not directory.is_dir():
                            continue
                        document = read_json(directory / DOCUMENT_FILE, {}) or {}
                        if not isinstance(document, dict):
                            continue
                        media_rel = document.get("media_path")
                        if not media_rel:
                            continue
                        try:
                            media_path = (stage / _safe_archive_path(media_rel)).resolve()
                            media_path.relative_to(stage.resolve())
                            media_present = media_path.is_file()
                        except (ProjectError, TypeError, ValueError):
                            media_present = False
                        if not media_present:
                            missing_media_documents.append(
                                document.get("id") or directory.name
                            )
                record["archive_import"] = {
                    "format_version": manifest.get("format_version", 1),
                    "integrity_verified": manifest["integrity_verified"],
                    "includes_media": manifest.get("includes_media"),
                    "missing_media_documents": missing_media_documents,
                }
                write_json(stage / PROJECT_FILE, record)

                for directory in ("media", "documents"):
                    (stage / directory).mkdir(parents=True, exist_ok=True)

                if target.exists():
                    # Replacing a project here keeps this computer's backup folder and
                    # history, but never an attestation, and nothing comes from the archive.
                    carried = _read_backup_file(target / BACKUP_FILE)
                    carried["attestation"] = None
                    if carried["destination"] or carried["history"]:
                        write_json(stage / BACKUP_FILE, carried)
                    backup = PATHS.projects / f".backup-{uuid4().hex}"
                    target.replace(backup)
                try:
                    stage.replace(target)
                except Exception:
                    if backup is not None and backup.exists():
                        backup.replace(target)
                    raise
                if backup is not None:
                    shutil.rmtree(backup)

                record["stats"] = project_stats(slug)
                record["imported_from"] = Path(archive_path).name
                record["archive_integrity_verified"] = manifest["integrity_verified"]
                record["archive_format_version"] = manifest.get("format_version", 1)
                record["archive_includes_media"] = manifest.get("includes_media")
                record["missing_media_documents"] = missing_media_documents
                return record
    except zipfile.BadZipFile as exc:
        raise ProjectError("That file is not a readable archive.") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


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
