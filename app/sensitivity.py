"""User-declared sensitivity and local-only policy for future backup transport."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_SENSITIVITY = "unknown_treat_as_phi"
SENSITIVITY_LABELS = {
    "no_phi": "No PHI in source media",
    "may_contain_phi": "Source media may contain PHI",
    "unknown_treat_as_phi": "Unknown — treat as PHI",
}
BOX_HEALTH_MARKER = "[Box Health - "


def validate_sensitivity(value) -> str:
    if not isinstance(value, str) or value not in SENSITIVITY_LABELS:
        raise ValueError("Select a valid project data sensitivity.")
    return value


def normalize_sensitivity(value) -> str:
    """Missing or unrecognized stored values must never relax safeguards."""
    try:
        return validate_sensitivity(value)
    except ValueError:
        return DEFAULT_SENSITIVITY


@dataclass(frozen=True)
class BackupGateResult:
    allowed: bool
    requires_box_health: bool
    recognized_box_health: bool
    override_used: bool
    reason: str


def future_backup_gate(
    classification,
    destination_root_name: str,
    *,
    independently_verified_box_health: bool = False,
) -> BackupGateResult:
    """Evaluate policy, not actual approval or availability; performs no I/O.

    Pass the selected Box Sync root's name, not a child folder or full path.
    Confirmation means the researcher independently verified an approved Box
    Health destination, never permission to use ordinary Box for PHI.
    A saved audit record is not confirmation for a later destination.
    """
    requires = normalize_sensitivity(classification) != "no_phi"
    recognized = (
        isinstance(destination_root_name, str)
        and "/" not in destination_root_name
        and "\\" not in destination_root_name
        and BOX_HEALTH_MARKER in destination_root_name
    )
    override = (
        requires and not recognized
        and independently_verified_box_health is True
        and isinstance(destination_root_name, str)
        and bool(destination_root_name.strip())
        and "/" not in destination_root_name
        and "\\" not in destination_root_name
    )
    if not requires:
        reason = "User declared no PHI; this policy does not require Box Health."
    elif recognized:
        reason = "Box Health root-name heuristic matched; this is not approval verification."
    elif override:
        reason = "Researcher explicitly confirmed independent verification of approved Box Health."
    else:
        reason = "Box Health required. Ordinary Box is not permitted for PHI or unknown data."
    return BackupGateResult(
        allowed=not requires or recognized or override,
        requires_box_health=requires,
        recognized_box_health=recognized,
        override_used=override,
        reason=reason,
    )
