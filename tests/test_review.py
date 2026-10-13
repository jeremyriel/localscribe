"""Standalone checks for the portable Review Mode queue.

Run with: python -m tests.test_review
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import review
from app.transcript import make_segment, make_word, new_document, normalise, apply_edits

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def sample_document():
    doc = new_document(duration=10, model="test")
    segment = make_segment(
        0, 1, 3, "synthetic words",
        [make_word("synthetic", 1, 2, 0.2), make_word("words", 2, 3, 0.95)],
    )
    segment["stale_timings"] = True
    segment["review"]["flagged"] = True
    segment["review"]["changed"] = True
    doc["segments"] = [segment]
    return normalise(doc)


def test_queue_categories_and_progress():
    doc = sample_document()
    items = review.queue_items(doc, artifacts=[0])
    check("all requested queue kinds are represented",
          {item["category"] for item in items}
          == {"low_confidence", "artifact", "unassigned", "changed", "flagged"})
    summary = review.queue_summary(items)
    check("all initial items are unresolved",
          summary["total"] == 5 and summary["open"] == 5 and summary["resolved"] == 0,
          str(summary))
    check("progress is complete for an empty queue",
          review.queue_summary([])["percent"] == 100)


def test_actions_and_portable_state():
    doc = sample_document()
    review.apply_action(doc, 0, "low_confidence", "resolve")
    review.apply_action(doc, 0, "artifact", "resolve")
    review.apply_action(doc, 0, "flagged", "resolve")

    items = review.queue_items(doc, artifacts=[0])
    completed = {item["category"]: item["resolved"] for item in items}
    check("resolved review items remain visible as completed",
          completed["low_confidence"] and completed["artifact"])
    check("clearing a flag removes it from the active queue",
          "flagged" not in completed)
    check("review completion is reflected in progress",
          review.queue_summary(items)["resolved"] == 2)
    check("changed-since-validation resolves independently",
          "changed" not in completed)

    review.apply_action(doc, 0, "flagged", "flag")
    restored = json.loads(json.dumps(doc))
    check("flags and resolutions survive portable JSON serialization",
          restored["segments"][0]["review"]["flagged"]
          and restored["segments"][0]["review"]["resolved"]
          == ["artifact", "low_confidence"])

    apply_edits(restored, [{"id": 0, "text": "corrected synthetic words"}])
    check("editing invalidates old resolutions but retains an explicit flag",
          restored["segments"][0]["review"]["resolved"] == []
          and restored["segments"][0]["review"]["flagged"]
          and restored["segments"][0]["review"]["changed"])


def main() -> int:
    for test in (test_queue_categories_and_progress, test_actions_and_portable_state):
        print(f"\n{test.__name__}")
        test()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
