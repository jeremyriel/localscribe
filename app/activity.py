"""Bounded, content-free history for privacy-sensitive actions."""

from __future__ import annotations

import json
import hashlib
import threading
from datetime import datetime, timezone

from . import config

HISTORY_LIMIT = 200
_LOCK = threading.Lock()

_RECOVERY_ACTIONS = {
    "document_delete",
    "import_replace",
    "import_replace_snapshot",
    "project_delete",
    "snapshot_create",
    "snapshot_restore",
}
_BACKUP_ACTIONS = {"box_sync_backup"}
_REMOTE_ACTIONS = {
    "box_sync_backup",
    "gpu_support_install",
    "model_download",
    "network_settings_changed",
    "offline_lock_changed",
    "update_apply",
    "update_check",
}
_ACTIONS = _RECOVERY_ACTIONS | _REMOTE_ACTIONS
_OUTCOMES = {
    "aborted",
    "cancelled",
    "complete",
    "completed",
    "deleted_with_snapshot",
    "deleted_without_snapshot",
    "engaged",
    "failed",
    "released",
    "restored_rename",
    "restored_replace",
    "replaced_no",
    "replaced_yes",
    "started",
}
LABELS = {
    "box_sync_backup": "Box Sync backup",
    "document_delete": "Document deletion",
    "gpu_support_install": "GPU support download",
    "import_replace": "Project import replacement",
    "import_replace_snapshot": "Pre-import recovery snapshot",
    "model_download": "Model download",
    "network_settings_changed": "Network and backup settings",
    "offline_lock_changed": "Offline Lock change",
    "project_delete": "Project deletion",
    "snapshot_create": "Recovery snapshot created",
    "snapshot_restore": "Recovery snapshot restored",
    "update_apply": "Application update",
    "update_check": "Update check",
}


def _project_ref(project_id: str) -> str:
    return hashlib.sha256(str(project_id).encode("utf-8")).hexdigest()[:24]


def category(action: str) -> str:
    if not isinstance(action, str):
        return "other"
    if action in _REMOTE_ACTIONS:
        return "network"
    if action in _RECOVERY_ACTIONS:
        return "recovery"
    return "other"


def label(action: str) -> str:
    if not isinstance(action, str):
        return "Other activity"
    return LABELS.get(action, "Other activity")


def groups(action) -> tuple[str, ...]:
    """Which history filters an action belongs to; a Box Sync backup is both."""
    found = []
    if action in _REMOTE_ACTIONS:
        found.append("network")
    if action in _RECOVERY_ACTIONS or action in _BACKUP_ACTIONS:
        found.append("recovery")
    return tuple(found)


def record(
    action: str, project_id: str | None = None, outcome: str = "completed"
) -> dict | None:
    """Persist an allow-listed event without titles, filenames, or paths."""
    path = config.PATHS.logs / "project-activity.json"
    action = action if isinstance(action, str) and action in _ACTIONS else "other"
    outcome = outcome if isinstance(outcome, str) and outcome in _OUTCOMES else "completed"
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                entries = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                entries = []
            if not isinstance(entries, list):
                entries = []
            entry = {
                "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "action": action,
                "category": category(action),
                "outcome": outcome,
            }
            if project_id is not None:
                entry["project_ref"] = _project_ref(project_id)
            entries.append(entry)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                json.dumps(entries[-HISTORY_LIMIT:], indent=2), encoding="utf-8"
            )
            temporary.replace(path)
            return entry
        except OSError:
            return None


def recent(project_id: str | None = None, limit: int = 50) -> list[dict]:
    count = max(0, min(int(limit), HISTORY_LIMIT))
    if not count:
        return []
    path = config.PATHS.logs / "project-activity.json"
    with _LOCK:
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(entries, list):
            return []
        result = [
            entry for entry in entries
            if isinstance(entry, dict)
            and (
                project_id is None
                or entry.get("project_ref") == _project_ref(project_id)
            )
        ]
        return result[-count:]


# ---------------------------------------------------------------------------
# Plain-language presentation (display only; nothing here is stored)
# ---------------------------------------------------------------------------

TYPE_LABELS = {"network": "Online", "recovery": "Data safety"}
STATUS_LABELS = {
    "success": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "started": "No result recorded",
}
_CANCELLED_OUTCOMES = {"aborted", "cancelled"}

_SUMMARIES = {
    "box_sync_backup": {
        "started": "Started a Box Sync backup",
        "success": "Backed up a project to your Box Sync folder",
        "failed": "A Box Sync backup failed",
        "cancelled": "A Box Sync backup was cancelled",
    },
    "document_delete": {
        "deleted_with_snapshot": "Removed a recording after saving a recovery snapshot",
        "deleted_without_snapshot": "Removed a recording without a recovery snapshot",
    },
    "gpu_support_install": {
        "started": "Started downloading GPU support",
        "success": "Downloaded GPU support",
        "failed": "GPU support download failed",
        "cancelled": "GPU support download was cancelled",
    },
    "import_replace": {
        "replaced_yes": "Replaced a project from an archive, after saving a snapshot of the old one",
        "replaced_no": "Replaced a project from an archive without a recovery snapshot",
    },
    "import_replace_snapshot": {
        "success": "Saved a recovery snapshot before an import replaced a project",
        "failed": "Could not save a recovery snapshot before an import",
    },
    "model_download": {
        "started": "Started downloading a speech model",
        "success": "Downloaded a speech model",
        "failed": "A speech model download failed",
        "cancelled": "A speech model download was cancelled",
    },
    "network_settings_changed": {"success": "Changed network or backup settings"},
    "offline_lock_changed": {
        "engaged": "Turned Offline Lock on",
        "released": "Turned Offline Lock off",
    },
    "project_delete": {
        "deleted_with_snapshot": "Deleted a project after saving a recovery snapshot",
        "deleted_without_snapshot": "Deleted a project without a recovery snapshot",
    },
    "snapshot_create": {
        "success": "Created a recovery snapshot",
        "failed": "Could not create a recovery snapshot",
    },
    "snapshot_restore": {
        "restored_rename": "Restored a snapshot as a separate project",
        "restored_replace": "Restored a snapshot over the existing project",
        "aborted": "Chose not to restore a snapshot; nothing changed",
    },
    "update_apply": {
        "started": "Started applying an app update",
        "success": "Applied an app update",
        "failed": "Applying an app update failed",
    },
    "update_check": {
        "started": "Started checking for updates",
        "success": "Checked for updates",
        "failed": "The update check failed",
    },
}


def status_of(outcome: str) -> str:
    if outcome == "failed":
        return "failed"
    if outcome in _CANCELLED_OUTCOMES:
        return "cancelled"
    if outcome == "started":
        return "started"
    return "success"


def summarize(action: str, outcome: str) -> str:
    phrases = _SUMMARIES.get(action, {})
    status = status_of(outcome)
    if outcome in phrases:
        return phrases[outcome]
    if status in phrases:
        return phrases[status]
    return f"{label(action)}: {str(outcome).replace('_', ' ')}"


def title_index(records) -> dict[str, str]:
    """Map one-way project references to titles of projects that still exist."""
    index = {}
    for record in records or []:
        project_id = record.get("project_id") or record.get("slug")
        if project_id and record.get("title"):
            index[_project_ref(project_id)] = str(record["title"])
    return index


def _local_time(value):
    try:
        return datetime.fromisoformat(str(value)).astimezone()
    except (TypeError, ValueError):
        return None


def _day_label(moment: datetime, today) -> str:
    days = (today - moment.date()).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return f"{moment:%b} {moment.day}, {moment.year}"


def _clock(moment: datetime) -> str:
    return f"{moment.hour % 12 or 12}:{moment:%M} {'AM' if moment.hour < 12 else 'PM'}"


def history_rows(events, titles: dict | None = None, now: datetime | None = None) -> list[dict]:
    """Turn stored events into readable rows, newest first.

    A "started" event and the result that follows it for the same action and
    project become one row. Titles come from `titles` (resolved by the caller
    for projects that still exist) and are never written back.
    """
    today = (now or datetime.now()).astimezone().date()
    rows: list[dict] = []
    pending: dict[tuple, dict] = {}
    for event in events or []:
        if not isinstance(event, dict):
            continue
        action = event.get("action")
        kind = event.get("category") or category(action)
        if kind not in TYPE_LABELS:
            continue
        outcome = str(event.get("outcome") or "completed")
        ref = event.get("project_ref")
        moment = _local_time(event.get("timestamp"))
        row = {
            "type": kind,
            "type_label": TYPE_LABELS[kind],
            "groups": " ".join(groups(action)),
            "day": _day_label(moment, today) if moment else "Unknown date",
            "time": _clock(moment) if moment else str(event.get("timestamp") or ""),
            "timestamp": event.get("timestamp"),
            "status": status_of(outcome),
            "summary": summarize(action, outcome),
            "detail": f"{action} · {outcome}",
            "project": None,
            "ref": str(ref)[:8] if ref else "",
        }
        if ref:
            row["project"] = (titles or {}).get(ref) or "A deleted or unknown project"
        key = (action, ref)
        if outcome == "started":
            pending[key] = row
            rows.append(row)
            continue
        earlier = pending.pop(key, None)
        if earlier is not None:
            rows.remove(earlier)
            row["detail"] += f" (started {earlier['time']})"
        rows.append(row)
    for row in rows:
        row["status_label"] = STATUS_LABELS[row["status"]]
    rows.reverse()
    return rows


def filter_counts(rows) -> dict[str, int]:
    """How many rows each history filter shows."""
    rows = list(rows or [])
    return {
        "all": len(rows),
        "network": sum("network" in row["groups"].split() for row in rows),
        "recovery": sum("recovery" in row["groups"].split() for row in rows),
    }
