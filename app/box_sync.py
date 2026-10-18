"""Explicit local archive copies into a researcher-configured Box Sync folder."""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import uuid
import zipfile
from pathlib import Path

from . import activity, config, projects
from .console import CONSOLE
from .sensitivity import future_backup_gate

MAX_JOBS = 50
COPY_CHUNK_BYTES = 1024 * 1024


class BackupError(Exception):
    """A local backup cannot safely start or complete."""


def _failure_message(exc: Exception) -> str:
    """A user-facing reason that never includes paths or project content."""
    if isinstance(exc, (zipfile.BadZipFile, zipfile.LargeZipFile)):
        return (
            "The project archive could not be built, so nothing was copied. "
            "Check the Local Scribe console for the error type."
        )
    if isinstance(exc, OSError):
        return (
            "The archive could not be written. Check that the Box Sync folder is "
            "writable and has free space, then retry."
        )
    return (
        "The backup stopped unexpectedly and nothing was copied. "
        "Check the Local Scribe console for the error type, then retry."
    )


def check_destination(raw) -> Path:
    """Validate a candidate backup folder and return its resolved path.

    Only that the folder exists, is a folder, is writable and lies outside
    Local Scribe's own storage; nothing here shows it is synchronized with Box
    or approved by an institution.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise BackupError("Choose a backup folder for this project first.")
    try:
        path = Path(raw.strip()).expanduser()
    except (OSError, ValueError, RuntimeError) as exc:
        raise BackupError("The backup folder path is invalid.") from exc
    if not path.is_absolute():
        raise BackupError("The backup folder must be an absolute path.")
    try:
        path = path.resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise BackupError("The backup folder is unavailable.") from exc
    if not path.is_dir():
        raise BackupError("The backup location is not a folder.")
    project_root = config.PATHS.projects.resolve()
    if path == project_root or project_root in path.parents:
        raise BackupError("Choose a backup folder outside Local Scribe's project storage.")
    if not os.access(path, os.W_OK):
        raise BackupError("The backup folder is not writable.")
    return path


def configured_destination(slug: str) -> Path:
    """This project's validated backup folder."""
    return check_destination(projects.load_backup_record(slug)["destination"])


def legacy_destination() -> str:
    """The folder an earlier version saved for every project, or ""."""
    raw = config.SETTINGS.get("box_sync_destination")
    return raw.strip() if isinstance(raw, str) else ""


def select_destination(slug: str, raw, selection: str = "manual") -> dict:
    """Validate and save a folder for one project; any failure changes nothing."""
    destination = check_destination(raw)
    return projects.set_backup_destination(slug, destination, selection)


def describe(slug: str) -> dict:
    """What the project page shows about this project's backup folder and history.

    The local copy is verified by checksum; whether Box has uploaded it is not
    something Local Scribe can see, so remote_sync is always "unknown".
    """
    project = projects.load_project(slug)
    record = projects.load_backup_record(slug)
    destination = record["destination"]
    state = {
        "configured": bool(destination),
        "destination": destination,
        "selection": record["selection"],
        "selected_at": record["selected_at"],
        "available": False,
        "problem": "",
        "needs_box_health": False,
        "legacy_available": bool(legacy_destination()) and not destination,
        "remote_sync": "unknown",
        "history": [
            {**event, "remote_sync": "unknown"}
            for event in reversed(record["history"])
        ][:10],
    }
    if destination:
        try:
            resolved = check_destination(destination)
        except BackupError as exc:
            state["problem"] = str(exc)
        else:
            state["available"] = True
            gate = future_backup_gate(
                project.get("data_sensitivity"), resolved.name,
                independently_verified_box_health=False,
            )
            state["needs_box_health"] = not gate.allowed
    return state


def validate_backup_request(
    slug: str,
    *,
    confirm_sync_destination: bool,
    independently_verified_box_health: bool,
) -> tuple[dict, Path]:
    if confirm_sync_destination is not True:
        raise BackupError(
            "Confirm that this selected folder belongs to your existing Box Sync client."
        )
    if config.SETTINGS.get("offline_lock"):
        raise BackupError(
            "Offline Lock is on. Turn it off before explicitly copying into a Box Sync folder."
        )
    project = projects.load_project(slug)
    destination = configured_destination(slug)
    gate = future_backup_gate(
        project.get("data_sensitivity"),
        destination.name,
        independently_verified_box_health=independently_verified_box_health,
    )
    if not gate.allowed:
        raise BackupError(gate.reason)
    if gate.override_used:
        projects.record_backup_attestation(
            slug,
            destination,
            independently_verified_box_health=True,
        )
    return project, destination


def _copy_and_verify(
    source: Path,
    target: Path,
    cancel_event: threading.Event,
    progress,
    check_allowed,
) -> int:
    partial = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
    source_digest = hashlib.sha256()
    total = 0
    try:
        with source.open("rb") as input_file, partial.open("xb") as output_file:
            for chunk in iter(lambda: input_file.read(COPY_CHUNK_BYTES), b""):
                if cancel_event.is_set():
                    raise projects.ProjectCancelled("Backup cancelled.")
                check_allowed()
                output_file.write(chunk)
                source_digest.update(chunk)
                total += len(chunk)
                progress(total)
            output_file.flush()

        copied_digest = hashlib.sha256()
        copied_size = 0
        with partial.open("rb") as copied_file:
            for chunk in iter(lambda: copied_file.read(COPY_CHUNK_BYTES), b""):
                if cancel_event.is_set():
                    raise projects.ProjectCancelled("Backup cancelled.")
                check_allowed()
                copied_digest.update(chunk)
                copied_size += len(chunk)
        if copied_size != total or copied_digest.digest() != source_digest.digest():
            raise BackupError("The copied archive did not pass checksum verification.")
        if cancel_event.is_set():
            raise projects.ProjectCancelled("Backup cancelled.")
        check_allowed()
        if target.exists():
            raise BackupError("A backup with that name already exists; retry to choose another name.")
        partial.replace(target)
        return total
    finally:
        partial.unlink(missing_ok=True)


class BackupManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}

    def _snapshot(self, job: dict) -> dict:
        snapshot = {
            key: job[key]
            for key in (
                "job_id", "status", "stage", "bytes_copied", "total_bytes",
                "verified", "filename", "error",
            )
        }
        # Verification is of the local copy only; Local Scribe cannot see Box.
        snapshot["local_copy_verified"] = bool(job["verified"])
        snapshot["remote_sync"] = "unknown"
        return snapshot

    def _prune(self) -> None:
        finished = [
            job_id for job_id, job in self._jobs.items()
            if job["status"] in ("complete", "failed", "cancelled")
        ]
        while len(self._jobs) >= MAX_JOBS and finished:
            self._jobs.pop(finished.pop(0), None)

    def submit(
        self,
        slug: str,
        *,
        confirm_sync_destination: bool,
        independently_verified_box_health: bool,
        include_media: bool = True,
    ) -> dict:
        project, destination = validate_backup_request(
            slug,
            confirm_sync_destination=confirm_sync_destination,
            independently_verified_box_health=independently_verified_box_health,
        )
        if not isinstance(include_media, bool):
            raise BackupError("'include_media' must be a boolean.")
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "project_id": project.get("project_id") or project["slug"],
            "slug": project["slug"],
            "destination": destination,
            "include_media": include_media,
            "cancel_event": threading.Event(),
            "status": "queued",
            "stage": "queued",
            "bytes_copied": 0,
            "total_bytes": None,
            "verified": False,
            "filename": None,
            "error": None,
            "thread": None,
        }
        with self._lock:
            self._prune()
            if len(self._jobs) >= MAX_JOBS:
                raise BackupError("Too many recent backup jobs; restart Local Scribe and retry.")
            self._jobs[job_id] = job
            thread = threading.Thread(
                target=self._run,
                args=(job_id,),
                name=f"box-sync-backup-{job_id[:8]}",
                daemon=True,
            )
            job["thread"] = thread
            thread.start()
            return self._snapshot(job)

    def _update(self, job_id: str, **changes) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(changes)

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
        status = "failed"
        final = {"status": "failed", "stage": "failed"}
        try:
            if job["cancel_event"].is_set():
                raise projects.ProjectCancelled("Backup cancelled.")
            if config.SETTINGS.get("offline_lock"):
                raise BackupError("Offline Lock was engaged; the backup was stopped.")
            if configured_destination(job["slug"]) != job["destination"]:
                raise BackupError("This project's backup folder changed; retry the backup.")

            def check_allowed() -> None:
                if job["cancel_event"].is_set():
                    raise projects.ProjectCancelled("Backup cancelled.")
                if config.SETTINGS.get("offline_lock"):
                    raise BackupError(
                        "Offline Lock was engaged; the backup was stopped."
                    )
                if configured_destination(job["slug"]) != job["destination"]:
                    raise BackupError(
                        "This project's backup folder changed; retry the backup."
                    )

            config.PATHS.logs.mkdir(parents=True, exist_ok=True)
            temporary_dir = config.PATHS.logs / f".box-backup-{job_id}"
            temporary_dir.mkdir()
            try:
                self._update(job_id, status="running", stage="building_archive")
                with projects._LOCK:
                    archive = projects.export_project(
                        job["slug"],
                        temporary_dir,
                        include_media=job["include_media"],
                        cancel_event=job["cancel_event"],
                    )
                if job["cancel_event"].is_set():
                    raise projects.ProjectCancelled("Backup cancelled.")
                if config.SETTINGS.get("offline_lock"):
                    raise BackupError("Offline Lock was engaged; the backup was stopped.")
                size = archive.stat().st_size
                filename = (
                    f"{projects.slugify(job['slug'])}-"
                    f"{uuid.uuid4().hex[:12]}{projects.EXPORT_SUFFIX}"
                )
                target = job["destination"] / filename
                self._update(
                    job_id,
                    stage="copying",
                    filename=filename,
                    total_bytes=size,
                )
                copied = _copy_and_verify(
                    archive,
                    target,
                    job["cancel_event"],
                    lambda count: self._update(job_id, bytes_copied=count),
                    check_allowed,
                )
                final = {
                    "status": "complete",
                    "stage": "complete",
                    "bytes_copied": copied,
                    "verified": True,
                    "error": None,
                }
                status = "complete"
            finally:
                shutil.rmtree(temporary_dir, ignore_errors=True)
        except projects.ProjectCancelled:
            final = {"status": "cancelled", "stage": "cancelled", "error": "Backup cancelled."}
            status = "cancelled"
        except BackupError as exc:
            final = {"status": "failed", "stage": "failed", "error": str(exc)}
        except Exception as exc:
            CONSOLE.error(f"Box Sync backup failed ({type(exc).__name__}).")
            final = {"status": "failed", "stage": "failed", "error": _failure_message(exc)}
        finally:
            activity.record(
                "box_sync_backup",
                job["project_id"],
                status,
            )
            projects.append_backup_event(job["slug"], {
                "status": status,
                "filename": job.get("filename") or "",
                "total_bytes": job.get("total_bytes") or 0,
                "local_copy_verified": status == "complete" and bool(final.get("verified")),
                "destination_name": job["destination"].name,
                "error": final.get("error") if status != "complete" else None,
            })
            self._update(job_id, **final)

    def get(self, slug: str, job_id: str | None = None) -> dict | None:
        with self._lock:
            if job_id is not None:
                job = self._jobs.get(job_id)
                return self._snapshot(job) if job and job["slug"] == slug else None
            matching = [
                job for job in self._jobs.values() if job["slug"] == slug
            ]
            if not matching:
                return None
            return self._snapshot(matching[-1])

    def cancel(self, slug: str, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if (
                job is None
                or job["slug"] != slug
                or job["status"] not in ("queued", "running")
            ):
                return False
            job["cancel_event"].set()
            return True


BACKUPS = BackupManager()
