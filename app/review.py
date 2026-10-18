"""Review queue state and derived items for transcript validation."""

from __future__ import annotations

from .transcript import LOW_CONFIDENCE, _prob_of

CATEGORIES = {
    "low_confidence": "Low confidence",
    "artifact": "Possible artifact",
    "unassigned": "Unassigned speaker",
    "flagged": "Flag",
    "changed": "Changed since validation",
}
RESOLVABLE = set(CATEGORIES) - {"flagged"}


def review_state(segment: dict) -> dict:
    """Return a repaired, portable review state stored with the segment."""
    state = segment.get("review")
    if not isinstance(state, dict):
        state = {}
    resolved = state.get("resolved")
    if not isinstance(resolved, list):
        resolved = []
    clean = {
        "flagged": bool(state.get("flagged")),
        "changed": bool(state.get("changed")),
        "resolved": sorted({
            str(category) for category in resolved
            if isinstance(category, str) and category in RESOLVABLE
        }),
    }
    segment["review"] = clean
    return clean


def queue_items(doc: dict, artifacts=()) -> list:
    """Return active queue entries, including completion state."""
    artifact_ids = {int(value) for value in artifacts if _integer(value)}
    items = []
    for segment in doc.get("segments") or []:
        segment_id = int(segment.get("id", 0))
        state = review_state(segment)
        active = []
        if any(_prob_of(word) < LOW_CONFIDENCE for word in segment.get("words") or []):
            active.append("low_confidence")
        if segment_id in artifact_ids:
            active.append("artifact")
        if not (segment.get("speaker_id") or "").strip():
            active.append("unassigned")
        if state["changed"]:
            active.append("changed")
        if state["flagged"]:
            active.append("flagged")

        for category in active:
            items.append({
                "segment_id": segment_id,
                "category": category,
                "label": CATEGORIES[category],
                "resolved": category in state["resolved"],
                "start": float(segment.get("start") or 0.0),
                "text": (segment.get("text") or "").strip(),
            })
    return items


def queue_summary(items: list) -> dict:
    counts = {
        category: {"total": 0, "resolved": 0, "open": 0}
        for category in CATEGORIES
    }
    for item in items:
        count = counts[item["category"]]
        count["total"] += 1
        count["resolved" if item.get("resolved") else "open"] += 1
    total = sum(value["total"] for value in counts.values())
    resolved = sum(value["resolved"] for value in counts.values())
    return {
        "total": total,
        "resolved": resolved,
        "open": total - resolved,
        "percent": round(100 * resolved / total) if total else 100,
        "categories": counts,
    }


def apply_action(doc: dict, segment_id, category: str, action: str) -> dict:
    if category not in CATEGORIES:
        raise ValueError("Unknown review category.")
    try:
        wanted_id = int(segment_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid segment id.") from exc
    segment = next(
        (item for item in doc.get("segments") or []
         if int(item.get("id", -1)) == wanted_id),
        None,
    )
    if segment is None:
        raise ValueError("Segment not found.")

    state = review_state(segment)
    if category == "flagged":
        if action == "flag":
            state["flagged"] = True
        elif action == "resolve":
            state["flagged"] = False
        else:
            raise ValueError("Flag actions must be 'flag' or 'resolve'.")
    elif action == "resolve":
        if category not in state["resolved"]:
            state["resolved"].append(category)
            state["resolved"].sort()
        state["changed"] = False
    else:
        raise ValueError("Review action must be 'resolve'.")
    return state


def _integer(value) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False
