"""Tests for the re-timestamp pass and the transcript document model.

Run with:  .venv/Scripts/python.exe -m tests.test_realign
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import transcript as T
from app.realign import realign_segment, retimestamp, tokenise
from app.transcript import apply_edits, make_segment, make_word, new_document


FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def build_doc():
    """A two-segment document with plausible word timings."""
    words1 = [
        make_word("The ", 0.00, 0.20, 0.99),
        make_word("quick ", 0.20, 0.55, 0.98),
        make_word("brown ", 0.55, 0.90, 0.40),
        make_word("fox ", 0.90, 1.30, 0.97),
    ]
    words2 = [
        make_word("jumps ", 2.00, 2.40, 0.95),
        make_word("over ", 2.40, 2.70, 0.93),
        make_word("the ", 2.70, 2.85, 0.99),
        make_word("lazy ", 2.85, 3.20, 0.88),
        make_word("dog ", 3.20, 3.60, 0.96),
    ]
    doc = new_document(duration=4.0, model="test", language="en", language_probability=0.99)
    doc["segments"] = [
        make_segment(0, 0.0, 1.30, "The quick brown fox", words1),
        make_segment(1, 2.0, 3.60, "jumps over the lazy dog", words2),
    ]
    return doc


def test_unchanged_text_preserves_timings():
    doc = build_doc()
    before = [(w["w"], w["start"], w["end"]) for _s, _i, w in T.all_words(doc)]
    report = retimestamp(doc)
    after = [(w["w"], w["start"], w["end"]) for _s, _i, w in T.all_words(doc)]
    check("no-op realign preserves every timing", before == after,
          f"\n   before={before[:2]}\n   after ={after[:2]}")
    check("no-op realign reports 100% exact", report["exact_pct"] == 100.0,
          f"got {report['exact_pct']}")
    check("no-op realign reports no interpolation",
          report["interpolated"] == 0 and report["redistributed"] == 0)


def test_single_word_correction():
    doc = build_doc()
    apply_edits(doc, [{"id": 0, "text": "The quick brown FOX"}])
    retimestamp(doc)
    words = doc["segments"][0]["words"]
    check("corrected word count unchanged", len(words) == 4, f"got {len(words)}")
    check("words before the edit keep exact timings",
          words[0]["start"] == 0.0 and words[1]["end"] == 0.55,
          f"got {words[:2]}")
    check("corrected word occupies the original span",
          abs(words[3]["start"] - 0.90) < 0.02 and abs(words[3]["end"] - 1.30) < 0.02,
          f"got {words[3]}")
    # Matching is on a normalised key, so a case or punctuation fix counts as
    # the same word and rightly keeps both its timing and its confidence.
    check("case-only fix keeps model confidence", words[3]["prob"] == 0.97,
          f"got {words[3]['prob']}")
    check("case-only fix adopts the new spelling", words[3]["w"] == "FOX",
          f"got {words[3]['w']!r}")


def test_true_replacement_drops_confidence():
    doc = build_doc()
    apply_edits(doc, [{"id": 0, "text": "The quick auburn fox"}])
    retimestamp(doc)
    words = doc["segments"][0]["words"]
    check("replaced word takes the new spelling", words[2]["w"] == "auburn",
          f"got {words[2]['w']!r}")
    check("replaced word occupies the original span",
          abs(words[2]["start"] - 0.55) < 0.02 and abs(words[2]["end"] - 0.90) < 0.02,
          f"got {words[2]}")
    check("replaced word loses model confidence", words[2]["prob"] == 0.0,
          f"got {words[2]['prob']}")
    check("untouched neighbours keep exact timings",
          words[1]["end"] == 0.55 and words[3]["start"] == 0.90,
          f"got {words[1]}, {words[3]}")


def test_insertion_interpolates():
    doc = build_doc()
    apply_edits(doc, [{"id": 0, "text": "The very quick brown fox"}])
    retimestamp(doc)
    words = doc["segments"][0]["words"]
    check("inserted word appears", len(words) == 5, f"got {[w['w'] for w in words]}")
    check("insertion lands between its anchors",
          words[0]["end"] <= words[1]["start"] < words[2]["start"],
          f"got {[(w['w'], w['start']) for w in words[:3]]}")
    check("timings stay monotonic after insertion",
          all(words[i]["end"] <= words[i + 1]["start"] + 1e-6 for i in range(len(words) - 1)),
          f"got {[(w['start'], w['end']) for w in words]}")


def test_deletion_collapses():
    doc = build_doc()
    apply_edits(doc, [{"id": 1, "text": "jumps over the dog"}])
    retimestamp(doc)
    words = doc["segments"][1]["words"]
    check("deleted word is gone", [w["w"].strip() for w in words] == ["jumps", "over", "the", "dog"],
          f"got {[w['w'] for w in words]}")
    check("surviving words keep exact timings",
          words[0]["start"] == 2.00 and words[2]["end"] == 2.85,
          f"got {words[:3]}")


def test_full_rewrite_is_flagged():
    doc = build_doc()
    apply_edits(doc, [
        {"id": 1, "text": "a completely different sentence was spoken here instead of that one"}
    ])
    report = retimestamp(doc)
    check("large rewrite is flagged for review", report["large_rewrites"] >= 1,
          f"got {report['large_rewrites']}")
    check("review segment ids reported", 1 in report["review_segments"],
          f"got {report['review_segments']}")
    words = doc["segments"][1]["words"]
    check("rewrite stays inside the segment span",
          words[0]["start"] >= 1.99 and words[-1]["end"] <= 4.01,
          f"got {words[0]['start']}..{words[-1]['end']}")


def test_edit_survives_normalise():
    doc = build_doc()
    apply_edits(doc, [{"id": 0, "text": "Totally different text"}])
    T.normalise(doc)
    check("normalise does not discard a human edit",
          T.segment_text(doc["segments"][0]) == "Totally different text",
          f"got {T.segment_text(doc['segments'][0])!r}")
    check("word count follows edited text", T.word_count(doc) == 3 + 5,
          f"got {T.word_count(doc)}")


def test_speaker_and_paragraphs():
    doc = build_doc()
    apply_edits(doc, [{"id": 0, "speaker": "Interviewer"}, {"id": 1, "speaker": "P01"}])
    check("speakers recorded", T.speakers(doc) == ["Interviewer", "P01"], f"got {T.speakers(doc)}")
    paras = T.paragraphs(doc, gap=1.5)
    check("speaker change starts a new paragraph", len(paras) == 2, f"got {len(paras)}")


def test_empty_segment_handled():
    doc = build_doc()
    apply_edits(doc, [{"id": 0, "text": ""}])
    report = retimestamp(doc)
    check("emptied segment does not crash realign", isinstance(report, dict))
    check("emptied segment has no words", doc["segments"][0]["words"] == [])


def test_segment_without_words():
    seg = make_segment(0, 5.0, 8.0, "three plain words", [])
    stats = realign_segment(seg, fallback_start=5.0, fallback_end=8.0)
    check("segment lacking word timings is spread", len(seg["words"]) == 3,
          f"got {seg['words']}")
    check("spread covers the segment span",
          seg["words"][0]["start"] == 5.0 and abs(seg["words"][-1]["end"] - 8.0) < 0.01,
          f"got {seg['words'][0]['start']}..{seg['words'][-1]['end']}")
    check("spread counted as interpolated", stats["interpolated"] == 3)


def test_tokenise():
    check("tokenise splits on whitespace",
          tokenise("  hello   world  ") == ["hello", "world"])
    check("tokenise keeps punctuation attached",
          tokenise("Well, yes -- indeed.") == ["Well,", "yes", "--", "indeed."])


def main() -> int:
    tests = [
        test_unchanged_text_preserves_timings,
        test_single_word_correction,
        test_true_replacement_drops_confidence,
        test_insertion_interpolates,
        test_deletion_collapses,
        test_full_rewrite_is_flagged,
        test_edit_survives_normalise,
        test_speaker_and_paragraphs,
        test_empty_segment_handled,
        test_segment_without_words,
        test_tokenise,
    ]
    for test in tests:
        print(f"\n{test.__name__}")
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
