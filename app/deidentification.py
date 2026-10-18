"""Local, reversible de-identification review assistance."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from . import projects
from .transcript import apply_edits

STATE_FILE = "deidentification.local.json"
DEFAULT_FLAGS = {
    "email": ("Email address", r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?!\w)"),
    "phone": ("Phone number", r"(?<!\w)(?:\+?\d[\d().\-\s]{5,}\d)(?!\w)"),
    "ssn": ("Possible SSN", r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    "date": ("Date", r"(?<!\d)(?:\d{1,2}[/-]){2}\d{2,4}(?!\d)"),
    "ip_address": ("IP address", r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"),
    "address": (
        "Possible street address",
        r"(?i)\b\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s+){0,5}"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr)\b",
    ),
    "url": ("Web address", r"(?i)\b(?:https?://|www\.)[^\s<>]+"),
}
MAX_CUSTOM_PATTERNS = 20
MAX_CUSTOM_PATTERN_LENGTH = 240
MAX_ITEMS = 2000


def _validate_custom_pattern(pattern: str) -> None:
    """Limit custom expressions to a small subset with bounded backtracking."""
    index = 0
    depth = 0
    previous_atom = None
    unbounded_repeats = 0
    bounded_repeats = 0

    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            if index + 1 >= len(pattern):
                raise ValueError("Custom patterns cannot end with an escape.")
            escaped = pattern[index + 1]
            if escaped.isdigit() or escaped in {"g", "k"}:
                raise ValueError("Backreferences are not allowed in custom patterns.")
            previous_atom = "atom"
            index += 2
            continue

        if char == "[":
            end = index + 1
            if end < len(pattern) and pattern[end] == "^":
                end += 1
            if end < len(pattern) and pattern[end] == "]":
                end += 1
            while end < len(pattern):
                if pattern[end] == "\\":
                    end += 2
                elif pattern[end] == "]":
                    break
                else:
                    end += 1
            if end >= len(pattern):
                raise ValueError("Custom patterns must have balanced character classes.")
            previous_atom = "atom"
            index = end + 1
            continue

        if char == "(":
            if pattern.startswith("(?", index) and not pattern.startswith("(?:", index):
                raise ValueError("Lookarounds and special groups are not allowed in custom patterns.")
            depth += 1
            if depth > 8:
                raise ValueError("Custom patterns may not nest more than 8 groups.")
            previous_atom = None
            index += 3 if pattern.startswith("(?:", index) else 1
            continue

        if char == ")":
            if depth == 0:
                raise ValueError("Custom patterns must have balanced groups.")
            depth -= 1
            previous_atom = "group"
            index += 1
            continue

        if char in "*+?{":
            if previous_atom != "atom":
                raise ValueError("Custom pattern quantifiers must follow a literal or character class.")
            if char in "*+":
                unbounded_repeats += 1
                if unbounded_repeats > 1:
                    raise ValueError("Use at most one unbounded repetition in a custom pattern.")
                index += 1
            elif char == "?":
                bounded_repeats += 1
                index += 1
            else:
                end = pattern.find("}", index + 1)
                if end < 0:
                    raise ValueError("Custom pattern repetitions must have a finite upper bound.")
                bounds = pattern[index + 1:end].split(",")
                try:
                    lower = int(bounds[0])
                    upper = int(bounds[-1])
                except (ValueError, IndexError) as exc:
                    raise ValueError("Custom pattern repetitions must have numeric bounds.") from exc
                if (
                    len(bounds) > 2 or lower < 0 or upper < lower
                    or upper > 100
                ):
                    raise ValueError("Custom pattern repetition bounds must be between 0 and 100.")
                bounded_repeats += upper
                index = end + 1
            if bounded_repeats > 1000:
                raise ValueError("Custom patterns contain too much bounded repetition.")
            previous_atom = None
            continue

        if char in "^$|":
            previous_atom = None
        else:
            previous_atom = "atom"
        index += 1

    if depth:
        raise ValueError("Custom patterns must have balanced groups.")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _state_path(slug: str, doc_id: str):
    return projects.document_dir(slug, doc_id) / STATE_FILE


def _empty_state() -> dict:
    return {
        "schema": 1,
        "flags": {name: True for name in DEFAULT_FLAGS},
        "custom_patterns": [],
        "items": [],
        "replacements": [],
    }


def load_state(slug: str, doc_id: str) -> dict:
    raw = projects.read_json(_state_path(slug, doc_id))
    state = _empty_state()
    if not isinstance(raw, dict):
        return state
    if isinstance(raw.get("flags"), dict):
        state["flags"].update({
            name: bool(raw["flags"].get(name, True))
            for name in DEFAULT_FLAGS
        })
    if isinstance(raw.get("custom_patterns"), list):
        try:
            state["custom_patterns"] = _clean_custom_patterns(raw["custom_patterns"])
        except ValueError:
            pass
    if isinstance(raw.get("items"), list):
        state["items"] = [item for item in raw["items"] if isinstance(item, dict)]
    if isinstance(raw.get("replacements"), list):
        state["replacements"] = [
            item for item in raw["replacements"] if isinstance(item, dict)
        ]
    return state


def _save_state(slug: str, doc_id: str, state: dict) -> None:
    projects.write_json(_state_path(slug, doc_id), state)


def _clean_custom_patterns(patterns) -> list[dict]:
    if not isinstance(patterns, list) or len(patterns) > MAX_CUSTOM_PATTERNS:
        raise ValueError(f"Use no more than {MAX_CUSTOM_PATTERNS} custom patterns.")
    clean = []
    for index, raw in enumerate(patterns):
        if not isinstance(raw, dict):
            raise ValueError("Each custom pattern needs a label and regular expression.")
        label = str(raw.get("label") or "").strip()[:80]
        pattern = str(raw.get("pattern") or "")
        if not label or not pattern or len(pattern) > MAX_CUSTOM_PATTERN_LENGTH:
            raise ValueError("Custom patterns need a label and a regex of 240 characters or fewer.")
        _validate_custom_pattern(pattern)
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid custom pattern '{label}': {exc}") from exc
        pattern_id = hashlib.sha256(f"{index}:{label}:{pattern}".encode()).hexdigest()[:12]
        clean.append({
            "id": pattern_id,
            "label": label,
            "pattern": pattern,
        })
    return clean


def _candidate_id(segment_id: int, pattern_id: str, start: int, end: int, text: str) -> str:
    key = f"{segment_id}:{pattern_id}:{start}:{end}:{text}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def _find_candidates(doc: dict, flags: dict, custom_patterns: list[dict]) -> list[dict]:
    patterns = [
        (name, label, expression)
        for name, (label, expression) in DEFAULT_FLAGS.items()
        if flags.get(name, True)
    ]
    patterns.extend(
        (f"custom:{item['id']}", item["label"], item["pattern"])
        for item in custom_patterns
    )
    results = []
    for segment in doc.get("segments") or []:
        segment_id = int(segment.get("id", 0))
        text = str(segment.get("text") or "")[:20_000]
        for category, label, expression in patterns:
            try:
                matches = re.finditer(expression, text, re.IGNORECASE)
                for match in matches:
                    value = match.group(0)
                    if not value.strip():
                        continue
                    results.append({
                        "id": _candidate_id(
                            segment_id, category, match.start(), match.end(), value,
                        ),
                        "segment_id": segment_id,
                        "category": category,
                        "label": label,
                        "text": value,
                        "start": match.start(),
                        "end": match.end(),
                        "status": "flagged",
                    })
                    if len(results) >= MAX_ITEMS:
                        return results
            except re.error:
                continue
    return results


def _report(state: dict) -> dict:
    items = state.get("items") or []
    counts = {
        status: sum(item.get("status") == status for item in items)
        for status in ("flagged", "resolved", "skipped")
    }
    return {
        "items": items,
        "counts": counts,
        "flags": state.get("flags") or {},
        "custom_patterns": state.get("custom_patterns") or [],
    }


def scan(slug: str, doc_id: str, doc: dict, flags=None, custom_patterns=None) -> dict:
    state = load_state(slug, doc_id)
    if flags is not None:
        if not isinstance(flags, dict):
            raise ValueError("Identifier flags must be an object.")
        state["flags"] = {
            name: bool(flags.get(name, False)) for name in DEFAULT_FLAGS
        }
    if custom_patterns is not None:
        state["custom_patterns"] = _clean_custom_patterns(custom_patterns)
    found = _find_candidates(doc, state["flags"], state["custom_patterns"])
    previous = {item.get("id"): item for item in state["items"]}
    seen = {item["id"] for item in found}
    for item in found:
        old = previous.get(item["id"])
        if old and old.get("status") in {"resolved", "skipped"}:
            item["status"] = old["status"]
            if old.get("replacement"):
                item["replacement"] = old["replacement"]
    for old in state["items"]:
        if old.get("id") not in seen:
            if old.get("status") == "flagged":
                old["status"] = "skipped"
                old["note"] = "No longer present in the latest scan."
            found.append(old)
    state["items"] = found
    _save_state(slug, doc_id, state)
    return _report(state)


def get_report(slug: str, doc_id: str) -> dict:
    return _report(load_state(slug, doc_id))


def skip(slug: str, doc_id: str, item_id: str) -> dict:
    state = load_state(slug, doc_id)
    item = next((row for row in state["items"] if row.get("id") == item_id), None)
    if item is None or item.get("status") != "flagged":
        raise ValueError("That flagged item is no longer available.")
    item["status"] = "skipped"
    _save_state(slug, doc_id, state)
    return _report(state)


def replace(slug: str, doc_id: str, doc: dict, item_id: str) -> tuple[dict, dict]:
    state = load_state(slug, doc_id)
    item = next((row for row in state["items"] if row.get("id") == item_id), None)
    if item is None or item.get("status") != "flagged":
        raise ValueError("That flagged item is no longer available.")
    segment = next(
        (row for row in doc.get("segments") or [] if int(row.get("id", -1)) == int(item["segment_id"])),
        None,
    )
    if segment is None:
        raise ValueError("The segment for this flagged item no longer exists.")
    text = str(segment.get("text") or "")
    start, end = int(item.get("start", -1)), int(item.get("end", -1))
    original = str(item.get("text") or "")
    if start < 0 or end > len(text) or text[start:end] != original:
        raise ValueError("This finding is stale; scan the transcript again.")
    stem = re.sub(r"[^A-Z0-9]+", "_", str(item.get("category") or "CUSTOM").upper())
    count = sum(
        str(row.get("replacement") or "").startswith(f"[{stem}_")
        for row in state["replacements"]
    ) + 1
    token = f"[{stem}_{count:03d}]"
    updated = text[:start] + token + text[end:]
    apply_edits(doc, [{"id": item["segment_id"], "text": updated}])
    item["status"] = "resolved"
    item["replacement"] = token
    state["replacements"].append({
        "item_id": item_id,
        "segment_id": int(item["segment_id"]),
        "category": item["category"],
        "original": original,
        "replacement": token,
        "start": start,
        "created_at": _now(),
        "status": "active",
    })
    delta = len(token) - len(original)
    for other in state["items"]:
        if (
            other is not item
            and other.get("status") == "flagged"
            and other.get("segment_id") == item["segment_id"]
        ):
            if int(other.get("start", 0)) >= end:
                other["start"] = int(other["start"]) + delta
                other["end"] = int(other["end"]) + delta
                other["id"] = _candidate_id(
                    int(other["segment_id"]), str(other["category"]),
                    int(other["start"]), int(other["end"]), str(other["text"]),
                )
            elif int(other.get("end", 0)) > start:
                other["status"] = "skipped"
                other["note"] = "Overlapped a replacement made for another finding."
    for row in state["items"]:
        if row is not item and row.get("id") == item_id and row.get("status") == "flagged":
            row["status"] = "skipped"
            row["note"] = "Covered by another replacement."
    _save_state(slug, doc_id, state)
    return doc, _report(state)


def undo(slug: str, doc_id: str, doc: dict, item_id: str) -> tuple[dict, dict]:
    state = load_state(slug, doc_id)
    mapping = next((
        row for row in reversed(state["replacements"])
        if row.get("item_id") == item_id and row.get("status") == "active"
    ), None)
    item = next((row for row in state["items"] if row.get("id") == item_id), None)
    if mapping is None or item is None:
        raise ValueError("That active replacement is no longer available.")
    segment = next(
        (row for row in doc.get("segments") or [] if int(row.get("id", -1)) == int(mapping["segment_id"])),
        None,
    )
    if segment is None:
        raise ValueError("The replacement text was edited; it cannot be safely undone.")
    text = str(segment.get("text") or "")
    token = str(mapping["replacement"])
    start = text.find(token)
    if start < 0 or text.find(token, start + len(token)) >= 0:
        raise ValueError("The replacement text was edited; it cannot be safely undone.")
    restored = (
        text[:start] + mapping["original"]
        + text[start + len(token):]
    )
    apply_edits(doc, [{"id": mapping["segment_id"], "text": restored}])
    delta = len(mapping["original"]) - len(token)
    for other in state["items"]:
        if (
            other is not item
            and other.get("status") == "flagged"
            and other.get("segment_id") == item["segment_id"]
            and int(other.get("start", 0)) >= start + len(token)
        ):
            other["start"] = int(other["start"]) + delta
            other["end"] = int(other["end"]) + delta
            other["id"] = _candidate_id(
                int(other["segment_id"]), str(other["category"]),
                int(other["start"]), int(other["end"]), str(other["text"]),
            )
    mapping["status"] = "undone"
    item["status"] = "flagged"
    item.pop("replacement", None)
    _save_state(slug, doc_id, state)
    return doc, _report(state)
