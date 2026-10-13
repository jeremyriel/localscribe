"""Checks for local transcript validation, reopening, and export provenance."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import validation
from app.transcript import new_document

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def test_validation_transitions_and_audit():
    doc = new_document(duration=1, model="test")
    check("new transcripts begin at Transcribed",
          validation.snapshot(doc)["status"] == "transcribed")
    validation.transition(doc, "start", "Reviewer A", "Beginning review",
                          at="2026-10-12T01:00:00+00:00")
    check("review starts explicitly",
          validation.snapshot(doc)["status"] == "in_review")
    try:
        validation.transition(doc, "validate", "", "Looks good")
    except ValueError:
        check("sign-off requires reviewer", True)
    else:
        check("sign-off requires reviewer", False)

    signed = validation.transition(
        doc, "validate", "Reviewer A", "Checked against audio",
        at="2026-10-12T02:00:00+00:00",
    )
    check("sign-off stores reviewer, time, and notes",
          signed["status"] == "validated"
          and signed["reviewer"] == "Reviewer A"
          and signed["validated_at"] == "2026-10-12T02:00:00+00:00"
          and signed["notes"] == "Checked against audio")
    try:
        validation.transition(doc, "reopen", "Reviewer B", "")
    except ValueError:
        check("reopening requires a reason", True)
    else:
        check("reopening requires a reason", False)

    reopened = validation.transition(
        doc, "reopen", "Reviewer B", "New source material arrived",
        at="2026-10-12T03:00:00+00:00",
    )
    check("reopening returns to review and remains in audit history",
          reopened["status"] == "in_review"
          and reopened["history"][-1]["action"] == "reopened"
          and reopened["history"][-1]["reviewer"] == "Reviewer B"
          and reopened["history"][-1]["notes"] == "New source material arrived")
    try:
        validation.require_reopened({
            **doc,
            "validation": {**doc["validation"], "status": "validated"},
        })
    except ValueError:
        check("validated transcript edits require deliberate reopening", True)
    else:
        check("validated transcript edits require deliberate reopening", False)


def test_export_stages_and_provenance():
    doc = new_document(duration=1, model="test")
    validation.record_export(doc, "2026-10-12T01:00:00+00:00", ["txt", "json"])
    check("early exports do not skip validation stages",
          validation.snapshot(doc)["status"] == "transcribed"
          and doc["export"]["status"] == "available")
    validation.transition(doc, "start", "Reviewer A")
    validation.transition(doc, "validate", "Reviewer A", "Approved")
    check("validated stage precedes export",
          validation.snapshot(doc)["status"] == "validated")
    validation.record_export(doc, "2026-10-12T02:00:00+00:00", ["txt", "json"])
    exported = validation.snapshot(doc)
    check("validated export advances to Exported",
          exported["status"] == "exported"
          and exported["export"]["formats"] == ["json", "txt"])
    provenance = validation.provenance(
        doc, "2026-10-12T02:00:00+00:00", ["txt", "json"],
    )
    check("export provenance carries validation and export details",
          provenance["status"] == "current"
          and provenance["workflow_status"] == "validated"
          and provenance["reviewer"] == "Reviewer A"
          and provenance["validated_at"]
          and provenance["exported_at"]
          and provenance["formats"] == ["json", "txt"])
    validation.transition(doc, "reopen", "Reviewer A", "Correction requested")
    check("reopening makes the old export non-current",
          validation.snapshot(doc)["status"] == "in_review"
          and doc["export"]["status"] == "stale")
    restored = validation.record_restore(doc, "earlier-revision")
    check("restoring content preserves the sign-off audit and invalidates export",
          [event["action"] for event in restored["history"]][-2:] ==
          ["reopened", "restored"]
          and restored["export"]["status"] == "stale")


def main() -> int:
    for test in (test_validation_transitions_and_audit, test_export_stages_and_provenance):
        print(f"\n{test.__name__}")
        test()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
