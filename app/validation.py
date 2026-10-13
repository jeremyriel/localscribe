"""Local transcript validation state and sign-off history."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


STAGES = ("draft", "transcribed", "in_review", "validated", "exported")
LABELS = {
    "draft": "Draft",
    "transcribed": "Transcribed",
    "in_review": "In review",
    "validated": "Validated",
    "exported": "Exported",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _state(doc: dict | None) -> dict:
    if doc is None:
        return {
            "status": "draft", "reviewer": "", "validated_at": None,
            "notes": "", "history": [],
        }
    state = doc.get("validation")
    if not isinstance(state, dict):
        state = {}
    allowed = {"transcribed", "in_review", "validated"}
    status = state.get("status")
    if status not in allowed:
        status = "in_review" if doc.get("human_edited") else "transcribed"
    history = state.get("history")
    if not isinstance(history, list):
        history = []
    clean_history = [
        event for event in history
        if isinstance(event, dict)
        and event.get("action") in {
            "started", "validated", "reopened", "exported", "restored",
        }
    ]
    state = {
        "status": status,
        "reviewer": str(state.get("reviewer") or ""),
        "validated_at": state.get("validated_at"),
        "notes": str(state.get("notes") or ""),
        "history": clean_history,
    }
    doc["validation"] = state
    export = doc.get("export")
    if not isinstance(export, dict):
        export = {}
    doc["export"] = {
        "status": export.get("status")
        if export.get("status") in {"available", "current", "stale"}
        else "not_exported",
        "exported_at": export.get("exported_at"),
        "formats": [
            str(fmt) for fmt in (export.get("formats") or [])
            if isinstance(fmt, str)
        ],
        "source_sha256": str(export.get("source_sha256") or ""),
    }
    return state


def _source_sha256(doc: dict) -> str:
    source = {key: value for key, value in doc.items() if key != "export"}
    encoded = json.dumps(
        source, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def current_status(doc: dict | None) -> str:
    if doc is None:
        return "draft"
    state = _state(doc)
    exported = doc["export"]
    if (
        state["status"] == "validated"
        and exported["status"] == "current"
        and exported["source_sha256"] == _source_sha256(doc)
    ):
        return "exported"
    return state["status"]


def snapshot(doc: dict | None) -> dict:
    state = _state(doc)
    status = current_status(doc)
    export = doc.get("export", {}) if doc else {}
    return {
        **state,
        "status": status,
        "label": LABELS[status],
        "stages": [{"id": stage, "label": LABELS[stage], "active": stage == status}
                   for stage in STAGES],
        "export": dict(export),
    }


def transition(
    doc: dict,
    action: str,
    reviewer: str = "",
    notes: str = "",
    at: str | None = None,
) -> dict:
    """Apply an explicit workflow transition and append it to the local audit."""
    state = _state(doc)
    reviewer = str(reviewer or "").strip()[:200]
    notes = str(notes or "").strip()[:4000]
    at = at or _now()

    if action == "start":
        if state["status"] == "transcribed":
            state["status"] = "in_review"
            state["history"].append({
                "action": "started", "reviewer": reviewer, "at": at, "notes": notes,
            })
        elif state["status"] != "in_review":
            raise ValueError("Reopen validation before starting another review.")
    elif action == "validate":
        if state["status"] != "in_review":
            raise ValueError("Start a review before validating this transcript.")
        if not reviewer:
            raise ValueError("Enter the reviewer’s name before signing off.")
        state.update({
            "status": "validated", "reviewer": reviewer,
            "validated_at": at, "notes": notes,
        })
        state["history"].append({
            "action": "validated", "reviewer": reviewer, "at": at, "notes": notes,
        })
    elif action == "reopen":
        if state["status"] != "validated":
            raise ValueError("Only a validated transcript can be reopened.")
        if not reviewer:
            raise ValueError("Enter the reviewer’s name to reopen validation.")
        if not notes:
            raise ValueError("Enter a reason before reopening validation.")
        state["status"] = "in_review"
        state["history"].append({
            "action": "reopened", "reviewer": reviewer, "at": at, "notes": notes,
        })
        if doc["export"]["exported_at"]:
            doc["export"]["status"] = "stale"
    else:
        raise ValueError("Unknown validation action.")

    doc["updated"] = at
    return snapshot(doc)


def record_export(doc: dict, at: str, formats: list[str]) -> dict:
    state = _state(doc)
    formats = sorted({str(fmt) for fmt in formats if isinstance(fmt, str)})
    state["history"].append({
        "action": "exported", "reviewer": state["reviewer"],
        "at": at, "notes": "", "formats": formats,
    })
    doc["export"] = {
        "status": "current" if state["status"] == "validated" else "available",
        "exported_at": at,
        "formats": formats,
        "source_sha256": "",
    }
    doc["export"]["source_sha256"] = _source_sha256(doc)
    return snapshot(doc)


def record_restore(doc: dict, revision_id: str, at: str | None = None) -> dict:
    state = _state(doc)
    at = at or _now()
    state["history"].append({
        "action": "restored",
        "reviewer": state["reviewer"],
        "at": at,
        "notes": f"Restored transcript revision {revision_id}.",
    })
    if doc["export"]["exported_at"]:
        doc["export"]["status"] = "stale"
    doc["updated"] = at
    return snapshot(doc)


def provenance(doc: dict, at: str, formats: list[str]) -> dict:
    state = _state(doc)
    return {
        "status": "current" if state["status"] == "validated" else "provisional",
        "workflow_status": state["status"],
        "reviewer": state["reviewer"],
        "validated_at": state["validated_at"],
        "exported_at": at,
        "formats": sorted({str(fmt) for fmt in formats if isinstance(fmt, str)}),
    }


def require_reopened(doc: dict) -> None:
    if _state(doc)["status"] == "validated":
        raise ValueError("Reopen validation deliberately before editing this transcript.")
