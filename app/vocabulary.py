"""Reusable local vocabulary profiles and transcription presets."""

from __future__ import annotations

import csv
import io
import json
import threading
from pathlib import Path
from uuid import uuid4

from .config import PATHS

_LOCK = threading.RLock()
_TARGETS = {"initial_prompt", "hotwords"}
_PRESET_SETTINGS = {
    "language": str,
    "task": {"transcribe", "translate"},
    "beam_size": (int, 1, 20),
    "best_of": (int, 1, 20),
    "multilingual": bool,
    "vad_filter": bool,
}
MAX_PROFILE_BYTES = 1_000_000
MAX_TERMS = 10_000


def _profile_file() -> Path:
    return PATHS.settings_file.parent / "vocabulary_profiles.json"


def _read() -> list[dict]:
    try:
        payload = json.loads(_profile_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [p for p in payload if isinstance(p, dict)] if isinstance(payload, list) else []


def _write(profiles: list[dict]) -> None:
    path = _profile_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _terms(values, *, required=True) -> list[str]:
    if values is None:
        values = []
    if isinstance(values, str):
        values = values.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not isinstance(values, (list, tuple)):
        raise ValueError("Vocabulary terms must be a list or text.")
    result, seen = [], set()
    for raw in values:
        term = str(raw or "").strip()
        if not term:
            continue
        if len(term) > 200:
            raise ValueError("Each vocabulary term must be 200 characters or fewer.")
        key = term.casefold()
        if key not in seen:
            result.append(term)
            seen.add(key)
    if not result and required:
        raise ValueError("Add at least one vocabulary term.")
    if len(result) > MAX_TERMS:
        raise ValueError(f"A profile can contain at most {MAX_TERMS:,} terms.")
    return result


def _settings(settings) -> dict:
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise ValueError("Preset settings must be an object.")
    result = {}
    for key, value in settings.items():
        spec = _PRESET_SETTINGS.get(key)
        if spec is None or value in ("", None):
            continue
        if key == "task":
            if value not in spec:
                raise ValueError("Preset task must be transcribe or translate.")
            result[key] = value
        elif key in ("beam_size", "best_of"):
            try:
                number = int(value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Preset {key} must be a whole number.") from exc
            if not spec[1] <= number <= spec[2]:
                raise ValueError(f"Preset {key} must be between {spec[1]} and {spec[2]}.")
            result[key] = number
        elif spec is bool:
            if not isinstance(value, bool):
                raise ValueError(f"Preset {key} must be true or false.")
            result[key] = value
        else:
            result[key] = str(value).strip()[:40]
    return result


def _public(profile: dict) -> dict:
    return {
        "id": profile["id"],
        "name": profile["name"],
        "target": profile["target"],
        "term_count": len(profile.get("terms") or []),
        "settings": dict(profile.get("settings") or {}),
    }


def list_profiles() -> list[dict]:
    with _LOCK:
        return [_public(p) for p in _read()]


def get_profile(profile_id: str | None) -> dict | None:
    if not profile_id:
        return None
    with _LOCK:
        return next((p for p in _read() if p.get("id") == profile_id), None)


def create_profile(name: str, terms, *, target="initial_prompt", settings=None) -> dict:
    name = str(name or "").strip()[:120]
    if not name:
        raise ValueError("A profile name is required.")
    if target not in _TARGETS:
        raise ValueError("Choose initial prompt or hotwords.")
    clean_settings = _settings(settings)
    clean_terms = _terms(terms, required=not bool(clean_settings))
    if not clean_terms and not clean_settings:
        raise ValueError("Add vocabulary terms or at least one preset setting.")
    profile = {
        "id": uuid4().hex,
        "name": name,
        "target": target,
        "terms": clean_terms,
        "settings": clean_settings,
    }
    with _LOCK:
        profiles = _read()
        profiles.append(profile)
        _write(profiles)
    return _public(profile)


def import_profile(name: str, filename: str, data: bytes, *, target="initial_prompt") -> dict:
    suffix = Path(str(filename or "")).suffix.casefold()
    if suffix not in {".csv", ".txt"}:
        raise ValueError("Choose a .csv or .txt vocabulary file.")
    if len(data) > MAX_PROFILE_BYTES:
        raise ValueError("Vocabulary files must be 1 MB or smaller.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Vocabulary files must use UTF-8 text.") from exc
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise ValueError("The CSV file could not be read.") from exc
    if suffix == ".csv" and rows:
        headers = [
            cell.strip().casefold().replace(" ", "_") for cell in rows[0]
        ]
        term_column = next(
            (index for index, value in enumerate(headers)
             if value in {
                 "term", "terms", "word", "words", "vocabulary",
                 "vocabulary_term", "term_name",
             }),
            None,
        )
        if term_column is not None:
            rows = [row[term_column:term_column + 1] for row in rows[1:]]
    return create_profile(name, [term for row in rows for term in row], target=target)


def transcription_overrides(
    settings: dict, project_glossary: str = "", profile: dict | None = None
) -> dict:
    """Combine project context, reusable terms, and per-study settings."""
    overrides = {}
    glossary = str(project_glossary or "").strip()
    global_prompt = str(settings.get("initial_prompt") or "").strip()
    if glossary and not global_prompt:
        overrides["initial_prompt"] = glossary
    if not profile:
        return overrides

    overrides.update(profile.get("settings") or {})
    target = profile.get("target")
    terms = profile.get("terms") or []
    if target not in _TARGETS or not terms:
        return overrides
    current = overrides.get(target, settings.get(target) or "")
    combined, seen = [], set()
    for term in [*str(current).split(","), *terms]:
        term = str(term).strip()
        if term and term.casefold() not in seen:
            combined.append(term)
            seen.add(term.casefold())
    overrides[target] = ", ".join(combined)
    return overrides


def redact_engine_params(params: dict) -> dict:
    """Keep vocabulary values out of persisted engine provenance."""
    clean = dict(params or {})
    for key in ("initial_prompt", "hotwords"):
        if key in clean:
            clean[key] = "[omitted]"
    return clean


def delete_profile(profile_id: str) -> bool:
    with _LOCK:
        profiles = _read()
        updated = [p for p in profiles if p.get("id") != profile_id]
        if len(updated) == len(profiles):
            return False
        _write(updated)
        return True
