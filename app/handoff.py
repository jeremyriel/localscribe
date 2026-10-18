"""Portable, explicitly transferred offline review packages."""

from __future__ import annotations

import hashlib
import json
import math
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import projects

FORMAT = "localscribe-review"
FORMAT_VERSION = 1
PACKAGE_SUFFIX = ".lsreview"
MAX_PACKAGE_BYTES = 4 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_INSTRUCTIONS_BYTES = 80_000
ANNOTATION_KINDS = {"comment", "question"}
REQUIRED_FILES = {
    "base_transcript.json", "transcript.json", "annotations.json", "instructions.txt",
}


class HandoffError(projects.ProjectError):
    """A review package could not be safely read or applied."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json_bytes(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def fingerprint(value) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def transcript_fingerprint(value) -> str:
    content = dict(value)
    content.pop("annotations", None)
    content.pop("updated", None)
    return fingerprint(content)


def _payload_metadata(value) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    if isinstance(value, Path):
        with value.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    else:
        size = len(value)
        digest.update(value)
    return size, digest.hexdigest()


def _manifest_file(name: str, data) -> dict:
    size, digest = _payload_metadata(data)
    return {"path": name, "size_bytes": size, "sha256": digest}


def clean_annotations(items, duration=None) -> list[dict]:
    """Validate portable annotation records and remove unexpected fields."""
    if not isinstance(items, list):
        raise HandoffError("Annotations must be a list.")
    try:
        max_duration = float(duration) if duration is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise HandoffError("Transcript duration is invalid.") from exc
    if max_duration is not None and not math.isfinite(max_duration):
        raise HandoffError("Transcript duration is invalid.")
    cleaned = []
    seen = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "")
        text = str(raw.get("text") or "").strip()
        if isinstance(raw.get("at"), bool):
            raise HandoffError("Each annotation needs a valid transcript timestamp.")
        try:
            at = float(raw.get("at"))
        except (TypeError, ValueError, OverflowError):
            raise HandoffError("Each annotation needs a valid transcript timestamp.")
        if (
            kind not in ANNOTATION_KINDS
            or not text
            or len(text) > 4000
            or not math.isfinite(at)
            or at < 0
            or (max_duration is not None and at > max_duration + 1)
        ):
            raise HandoffError("A comment or question is invalid or outside the transcript.")
        annotation_id = str(raw.get("id") or uuid4().hex)[:80]
        if not annotation_id or annotation_id in seen:
            annotation_id = uuid4().hex
        seen.add(annotation_id)
        created_at = str(raw.get("created_at") or _now())[:80]
        try:
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            created_at = _now()
        segment_id = raw.get("segment_id")
        if segment_id is not None:
            try:
                segment_id = int(segment_id)
            except (TypeError, ValueError):
                segment_id = None
        cleaned.append({
            "id": annotation_id,
            "kind": kind,
            "text": text,
            "at": round(at, 3),
            "segment_id": segment_id,
            "created_at": created_at,
            "author": str(raw.get("author") or "")[:200],
        })
    return cleaned


def merge_annotations(*groups, duration=None) -> list[dict]:
    """Merge annotations by stable ID, keeping the first copy of each."""
    merged = []
    seen = set()
    for group in groups:
        for item in clean_annotations(group or [], duration):
            if item["id"] not in seen:
                merged.append(item)
                seen.add(item["id"])
    return merged


def package_annotations(base, incoming, annotations, duration=None) -> list[dict]:
    """Reconcile the duplicated editable transcript and annotations payloads."""
    baseline = {
        item["id"]: item
        for item in clean_annotations(base or [], duration)
    }
    transcript_items = {
        item["id"]: item
        for item in clean_annotations(incoming or [], duration)
    }
    annotation_items = {
        item["id"]: item
        for item in clean_annotations(annotations or [], duration)
    }
    merged = []
    for annotation_id in dict.fromkeys((*annotation_items, *transcript_items)):
        from_file = annotation_items.get(annotation_id)
        from_transcript = transcript_items.get(annotation_id)
        original = baseline.get(annotation_id)
        if from_file is None:
            selected = from_transcript
        elif from_transcript is None:
            selected = from_file
        elif original is None:
            selected = from_file
        elif from_transcript != original:
            selected = from_transcript
        else:
            selected = from_file
        if selected is not None:
            merged.append(selected)
    return merged


def create_package(
    slug: str,
    doc_id: str,
    dest_dir: Path,
    *,
    include_media: bool = False,
    instructions: str = "",
) -> Path:
    project = projects.load_project(slug)
    document = projects.load_document(slug, doc_id)
    transcript = projects.load_transcript(slug, doc_id)
    if transcript is None:
        raise HandoffError("This document has no transcript to review.")
    instructions = str(instructions or "").strip()
    if len(instructions) > 20000:
        raise HandoffError("Instructions must be 20,000 characters or fewer.")
    transcript = dict(transcript)
    annotations = clean_annotations(
        transcript.get("annotations") or [], transcript.get("duration")
    )
    transcript["annotations"] = annotations
    base = _json_bytes(transcript)
    transcript_data = _json_bytes(transcript)
    payloads = {
        "base_transcript.json": base,
        "transcript.json": transcript_data,
        "annotations.json": _json_bytes(annotations),
        "instructions.txt": instructions.encode("utf-8"),
    }
    if include_media:
        try:
            media_path = projects.media_file(slug, doc_id)
        except projects.ProjectError as exc:
            raise HandoffError("Source media is not available for this recording.") from exc
        media_name = Path(document.get("media_filename") or document.get("filename") or "media")
        suffix = re.sub(r"[^A-Za-z0-9._-]", "_", media_name.name)[:150] or "media"
        payloads[f"media/{suffix}"] = media_path

    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "created": _now(),
        "project_id": project.get("project_id") or project["slug"],
        "project_title": project.get("title") or "",
        "document_id": doc_id,
        "filename": document.get("filename") or "",
        "includes_media": bool(include_media),
        "base_sha256": hashlib.sha256(base).hexdigest(),
        "files": [_manifest_file(name, data) for name, data in sorted(payloads.items())],
    }
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = projects.slugify(Path(document.get("filename") or doc_id).stem)
    package_path = dest_dir / f"{stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}{PACKAGE_SUFFIX}"
    try:
        with zipfile.ZipFile(package_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in payloads.items():
                if isinstance(data, Path):
                    projects.write_zip_file(archive, data, name)
                else:
                    archive.writestr(name, data)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
    except Exception:
        package_path.unlink(missing_ok=True)
        raise
    return package_path


def read_package(package_path: Path) -> dict:
    package_path = Path(package_path)
    try:
        if package_path.stat().st_size > MAX_PACKAGE_BYTES:
            raise HandoffError("Review package is larger than the 4 GiB limit.")
        with zipfile.ZipFile(package_path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise HandoffError("Review package has duplicate paths or no manifest.")
            for name in names:
                if name.endswith("/"):
                    raise HandoffError("Review package must not contain directory entries.")
                projects._safe_archive_path(name)
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > MAX_JSON_BYTES:
                raise HandoffError("Review package manifest is too large.")
            manifest = json.loads(archive.read("manifest.json"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != FORMAT
                or manifest.get("format_version") != FORMAT_VERSION
                or not isinstance(manifest.get("includes_media"), bool)
                or not manifest.get("project_id")
                or not manifest.get("document_id")
            ):
                raise HandoffError("Unsupported or invalid review package format.")
            entries = manifest.get("files")
            if not isinstance(entries, list):
                raise HandoffError("Review package has no valid file list.")
            listed = {}
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    raise HandoffError("Review package file list is invalid.")
                name = entry["path"]
                projects._safe_archive_path(name)
                if name in listed or name == "manifest.json":
                    raise HandoffError("Review package file list contains duplicates.")
                listed[name] = entry
            extras = set(listed) - REQUIRED_FILES
            if any(not name.startswith("media/") or name.count("/") != 1 for name in extras):
                raise HandoffError("Review package contains unsupported files.")
            if set(names) != set(listed) | {"manifest.json"}:
                raise HandoffError("Review package contents do not match its manifest.")
            media_names = {name for name in listed if name.startswith("media/")}
            if (
                (manifest["includes_media"] and len(media_names) != 1)
                or (not manifest["includes_media"] and media_names)
            ):
                raise HandoffError("Review package media does not match its manifest.")
            data = {}
            total = 0
            for name, entry in listed.items():
                info = archive.getinfo(name)
                total += info.file_size
                if total > MAX_PACKAGE_BYTES:
                    raise HandoffError("Review package contents exceed the 4 GiB limit.")
                if name.endswith(".json") and info.file_size > MAX_JSON_BYTES:
                    raise HandoffError("Review package JSON content is too large.")
                if name == "instructions.txt" and info.file_size > MAX_INSTRUCTIONS_BYTES:
                    raise HandoffError("Review package instructions are too large.")
                digest = hashlib.sha256()
                payload = bytearray() if not name.startswith("media/") else None
                size = 0
                with archive.open(info) as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(chunk)
                        digest.update(chunk)
                        if payload is not None:
                            payload.extend(chunk)
                editable = name in {"transcript.json", "annotations.json"}
                if not editable and (
                    size != entry.get("size_bytes")
                    or digest.hexdigest() != entry.get("sha256")
                ):
                    raise HandoffError(f"Review package integrity check failed for {name}.")
                if payload is not None:
                    data[name] = bytes(payload)
    except HandoffError:
        raise
    except (
        OSError, zipfile.BadZipFile, KeyError, ValueError, RuntimeError,
        projects.ProjectError,
    ) as exc:
        raise HandoffError("Could not read this review package.") from exc

    if not REQUIRED_FILES.issubset(data):
        raise HandoffError("Review package is missing required review files.")
    try:
        base = json.loads(data["base_transcript.json"])
        incoming = json.loads(data["transcript.json"])
        annotations = json.loads(data["annotations.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffError("Review package transcript data is not valid UTF-8 JSON.") from exc
    if (
        not isinstance(base, dict)
        or not isinstance(incoming, dict)
        or not isinstance(incoming.get("segments"), list)
        or fingerprint(base) != manifest.get("base_sha256")
    ):
        raise HandoffError("Review package transcript or baseline is invalid.")
    annotations = package_annotations(
        base.get("annotations") or [],
        incoming.get("annotations") or [],
        annotations,
        duration=incoming.get("duration"),
    )
    incoming["annotations"] = annotations
    return {
        "manifest": manifest,
        "base": base,
        "incoming": incoming,
        "annotations": annotations,
        "includes_media": bool(manifest.get("includes_media")),
    }


def import_package(package_path: Path, slug: str, doc_id: str) -> dict:
    package = read_package(package_path)
    manifest = package["manifest"]
    project = projects.load_project(slug)
    document = projects.load_document(slug, doc_id)
    if (
        str(manifest.get("project_id") or "") != str(project.get("project_id") or project["slug"])
        or str(manifest.get("document_id") or "") != str(doc_id)
    ):
        raise HandoffError("This review package belongs to a different project or recording.")
    current = projects.load_transcript(slug, doc_id)
    if current is None:
        raise HandoffError("This recording no longer has a transcript.")
    incoming = package["incoming"]
    current_annotation_ids = {
        str(item.get("id")) for item in current.get("annotations") or []
        if isinstance(item, dict)
    }
    annotations_added = sum(
        1 for item in package["annotations"] if item["id"] not in current_annotation_ids
    )
    incoming["annotations"] = merge_annotations(
        package["annotations"], incoming.get("annotations") or [],
        current.get("annotations") or [], duration=current.get("duration"),
    )
    base_fp = transcript_fingerprint(package["base"])
    current_fp = transcript_fingerprint(current)
    incoming_fp = transcript_fingerprint(incoming)
    if current_fp == incoming_fp or incoming_fp == base_fp:
        if current.get("annotations") != incoming.get("annotations"):
            current["annotations"] = incoming["annotations"]
            projects.save_transcript(slug, doc_id, current, revision=False)
        return {"conflict": None, "annotations_added": annotations_added}
    if current_fp == base_fp:
        projects.save_transcript(
            slug, doc_id, incoming,
            keep=20,
        )
        return {"conflict": None, "annotations_added": annotations_added}

    conflict_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    conflict_dir = projects.document_dir(slug, doc_id) / "conflicts" / conflict_id
    conflict_dir.mkdir(parents=True, exist_ok=False)
    projects.write_json(conflict_dir / "base-transcript.json", package["base"])
    projects.write_json(conflict_dir / "local-transcript.json", current)
    projects.write_json(conflict_dir / "incoming-transcript.json", incoming)
    projects.write_json(conflict_dir / "conflict.json", {
        "id": conflict_id,
        "created_at": _now(),
        "status": "unresolved",
        "project_id": project.get("project_id"),
        "document_id": doc_id,
        "package_created": manifest.get("created"),
        "incoming_filename": manifest.get("filename") or document.get("filename"),
        "includes_media": package["includes_media"],
    })
    current["annotations"] = incoming["annotations"]
    projects.save_transcript(slug, doc_id, current, revision=False)
    return {
        "conflict": conflict_id,
        "annotations_added": annotations_added,
    }


def list_conflicts(slug: str, doc_id: str) -> list[dict]:
    root = projects.document_dir(slug, doc_id) / "conflicts"
    conflicts = []
    if not root.is_dir():
        return conflicts
    for path in sorted(root.iterdir(), reverse=True):
        record = projects.read_json(path / "conflict.json")
        if isinstance(record, dict):
            conflicts.append(record)
    return conflicts


def conflict_file(slug: str, doc_id: str, conflict_id: str, version: str) -> Path:
    if (
        not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}", str(conflict_id))
        or version not in ("local", "incoming")
    ):
        raise HandoffError("Invalid conflict version.")
    path = (
        projects.document_dir(slug, doc_id) / "conflicts" / conflict_id
        / f"{version}-transcript.json"
    )
    if not path.is_file():
        raise HandoffError("That conflict version was not found.")
    return path


def _conflict_directory(slug: str, doc_id: str, conflict_id: str) -> Path:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}", str(conflict_id)):
        raise HandoffError("Invalid conflict identifier.")
    return projects.document_dir(slug, doc_id) / "conflicts" / conflict_id


def resolve_conflict(slug: str, doc_id: str, conflict_id: str, choice: str) -> dict:
    if choice not in ("local", "incoming"):
        raise HandoffError("Choose either the current or imported transcript.")
    path = _conflict_directory(slug, doc_id, conflict_id)
    record = projects.read_json(path / "conflict.json")
    if (
        not isinstance(record, dict)
        or record.get("id") != conflict_id
        or record.get("document_id") != doc_id
        or record.get("status") != "unresolved"
    ):
        raise HandoffError("That transcript conflict is not unresolved.")
    if choice == "incoming":
        incoming = projects.read_json(path / "incoming-transcript.json")
        current = projects.load_transcript(slug, doc_id) or {}
        if not isinstance(incoming, dict):
            raise HandoffError("The imported transcript version is missing.")
        incoming["annotations"] = merge_annotations(
            incoming.get("annotations") or [], current.get("annotations") or [],
            duration=incoming.get("duration"),
        )
        projects.save_transcript(slug, doc_id, incoming, keep=20)
    record["status"] = "resolved"
    record["resolution"] = choice
    record["resolved_at"] = _now()
    projects.write_json(path / "conflict.json", record)
    return record
