"""Build a deliberately small, path-free diagnostics document."""

from __future__ import annotations

import platform
import re
from datetime import datetime, timezone

from . import config, projects

_SAFE_TEXT = re.compile(r"[0-9A-Za-z._+ -]{1,32}")


def _safe_text(value) -> str:
    """A short platform label, or "unknown" if it is not plainly a version-like token."""
    return value if isinstance(value, str) and _SAFE_TEXT.fullmatch(value) else "unknown"


def build(engine_status: dict | None = None) -> dict:
    """Return only reviewed, non-content runtime facts for user inspection."""
    engine = engine_status or {}
    version = config.app_version()
    version = version if re.fullmatch(r"[0-9A-Za-z.+-]{1,32}", version) else "unknown"
    raw_device = engine.get("target_device")
    safe_device = None
    if isinstance(raw_device, str):
        if raw_device in {"auto", "cpu", "mlx"}:
            safe_device = raw_device
        elif re.fullmatch(r"cuda:\d{1,2}", raw_device):
            safe_device = raw_device
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"
        ),
        "application": {
            "name": config.APP_NAME,
            "version": version,
        },
        "runtime": {
            "operating_system": platform.system(),
            # Platform and CPU architecture decide which GPU path and which
            # native wheels apply, which is most of what support needs to know.
            "operating_system_release": _safe_text(platform.release()),
            "architecture": _safe_text(platform.machine()),
            "python_version": platform.python_version(),
        },
        "privacy": {
            "offline_lock": bool(config.SETTINGS.get("offline_lock")),
            "update_checks_enabled": bool(
                config.SETTINGS.get("check_for_updates", True)
            ),
            "box_sync_backup_configured": bool(
                config.SETTINGS.get("box_sync_destination")
            ) or projects.any_backup_destination(),
        },
        "engine": {
            "state": engine.get("state")
            if isinstance(engine.get("state"), str)
            and engine.get("state") in {"ready", "loading", "unloaded", "error"}
            else "unknown",
            "device": safe_device,
            "model_downloaded": bool(engine.get("model_downloaded", False)),
        },
    }
