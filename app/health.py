"""Local-only checks for the app's data directory, models, storage, and runtime."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from . import catalog
from .config import PATHS

MIN_FREE_BYTES = 1_000_000_000


def _check(name: str, label: str, status: str, detail: str, **values) -> dict:
    return {
        "name": name,
        "label": label,
        "status": status,
        "detail": detail,
        **values,
    }


def _data_directory_check(paths) -> dict:
    required = (
        paths.models,
        paths.projects,
        paths.logs,
        paths.settings_file.parent,
    )
    for path in required:
        try:
            if not path.is_dir() or not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                return _check(
                    "data_directory", "Data directory", "error",
                    "One or more local data folders are missing or inaccessible. "
                    "Check the folder and its permissions.",
                )
        except OSError:
            return _check(
                "data_directory", "Data directory", "error",
                "One or more local data folders could not be inspected. "
                "Check the folder and its permissions.",
            )
    try:
        if paths.settings_file.exists() and (
            not paths.settings_file.is_file()
            or not os.access(paths.settings_file, os.R_OK | os.W_OK)
        ):
            return _check(
                "data_directory", "Data directory", "error",
                "The settings file is inaccessible. Check the file and its permissions.",
            )
    except OSError:
        return _check(
            "data_directory", "Data directory", "error",
            "The settings file could not be inspected. Check the file and its permissions.",
        )
    return _check(
        "data_directory", "Data directory", "ok",
        "Local data folders are accessible.",
    )


def _model_data_check(paths, model_key: str) -> dict:
    spec = catalog.model_spec(model_key)
    if spec is None:
        return _check(
            "model_data", "Selected model data", "error",
            "The selected model is not in the supported model catalogue.",
        )

    repo_dir = Path(paths.models) / (
        "models--" + spec.repo.replace("/", "--")
    )
    try:
        if not repo_dir.exists():
            return _check(
                "model_data", "Selected model data", "warning",
                f"{spec.label} is not downloaded yet.",
                model=model_key,
            )
        snapshots = repo_dir / "snapshots"
        if not snapshots.is_dir():
            raise OSError("snapshot directory is missing")
        marker = "config.json" if spec.engine == "mlx" else "model.bin"
        for snapshot in snapshots.iterdir():
            marker_path = snapshot / marker
            if not snapshot.is_dir() or not marker_path.is_file():
                continue
            if marker_path.stat().st_size <= 0:
                continue
            config_path = snapshot / "config.json"
            try:
                with config_path.open(encoding="utf-8") as config_file:
                    model_config = json.load(config_file)
            except (OSError, ValueError):
                continue
            if not isinstance(model_config, dict):
                continue
            if spec.engine == "mlx" and not any(
                weight.is_file() and weight.stat().st_size > 0
                for pattern in ("*.safetensors", "*.npz")
                for weight in snapshot.glob(pattern)
            ):
                continue
            return _check(
                "model_data", "Selected model data", "ok",
                f"{spec.label} has a local model snapshot.",
                model=model_key,
            )
    except OSError:
        pass

    return _check(
        "model_data", "Selected model data", "error",
        f"{spec.label} data is incomplete or unreadable. Delete it from AI "
        "Settings and download it again.",
        model=model_key,
    )


def _storage_check(paths, model_key: str) -> dict:
    spec = catalog.model_spec(model_key)
    estimate = catalog.disk_estimate(model_key)
    model_needs_download = _model_data_check(paths, model_key)["status"] != "ok"
    required_bytes = max(
        MIN_FREE_BYTES,
        estimate["recommended_free_bytes"]
        if spec and model_needs_download else MIN_FREE_BYTES,
    )
    try:
        free_bytes = shutil.disk_usage(paths.models).free
    except OSError:
        return _check(
            "storage", "Available storage", "error",
            "Free space could not be checked for the model storage volume.",
        )
    status = "warning" if free_bytes < required_bytes else "ok"
    detail = (
        f"{free_bytes / 1_000_000_000:.1f} GB is free; "
        f"keep at least {required_bytes / 1_000_000_000:.1f} GB free "
        "for the selected model and normal app use."
        if status == "warning"
        else f"{free_bytes / 1_000_000_000:.1f} GB is free on the model storage volume."
    )
    return _check(
        "storage", "Available storage", status, detail,
        free_bytes=free_bytes,
        required_bytes=required_bytes,
    )


def _runtime_check(version_info=None) -> dict:
    version = version_info if version_info is not None else sys.version_info
    version_tuple = tuple(version[:3])
    supported = (3, 10, 0) <= version_tuple < (3, 14, 0)
    label = ".".join(str(part) for part in version_tuple)
    return _check(
        "runtime", "Python runtime", "ok" if supported else "error",
        f"Python {label} is supported."
        if supported
        else f"Python {label} is unsupported. Local Scribe requires Python 3.10–3.13.",
        version=label,
    )


def run(paths=None, model_key: str | None = None) -> dict:
    """Inspect local state only; this function makes no network requests."""
    paths = paths or PATHS
    if model_key is None:
        from .config import SETTINGS
        model_key = SETTINGS.get("model")
    return {
        "checks": [
            _storage_check(paths, model_key),
            _data_directory_check(paths),
            _model_data_check(paths, model_key),
            _runtime_check(),
        ],
        "local_only": True,
    }
