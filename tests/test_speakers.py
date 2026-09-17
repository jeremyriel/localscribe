"""Tests for the speaker roster and the speaking-turn model.

The central rule under test: a speaker's name is printed when the speaker
changes, and NOT reprinted after a pause when the same person is still
talking. That rule is what makes a transcript read like a transcript.

Run with:  .venv/Scripts/python.exe -m tests.test_speakers
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import transcript as T
from app.transcript import (
    PALETTE,
    assign_speaker,
    clean_roster,
    derive_short,
    make_segment,
    make_word,
    migrate_speakers,
    new_document,
    new_speaker,
    normalise,
    set_roster,
    turn_blocks,
    turns,
)

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def seg(index, start, end, text, speaker_id="", speaker=""):
    words = []
    span = (end - start) / max(1, len(text.split()))
    cursor = start
    for token in text.split():
        words.append(make_word(token, cursor, cursor + span * 0.9, 0.95))
        cursor += span
    return make_segment(index, start, end, text, words,
                        speaker_id=speaker_id, speaker=speaker)


def doc_with(segments, speakers=None):
    doc = new_document(duration=120.0, model="test", language="en")
    doc["speakers"] = speakers if speakers is not None else []
    doc["segments"] = segments
    return doc


def two_speakers():
    return [
        {"id": "s1", "name": "Interviewer", "short": "INT", "color": PALETTE[0]},
        {"id": "s2", "name": "Participant 04", "short": "P04", "color": PALETTE[1]},
    ]


# ---------------------------------------------------------------------------

def test_derive_short():
    check("two words give initials", derive_short("Ana Ruiz") == "AR",
          derive_short("Ana Ruiz"))
    check("trailing number is kept whole",
          derive_short("Participant 04") == "P04", derive_short("Participant 04"))
    check("single word is compressed", derive_short("Interviewer") == "INT",
          derive_short("Interviewer"))
    check("collisions are avoided",
          derive_short("Ana Ruiz", taken=["AR"]) != "AR",
          derive_short("Ana Ruiz", taken=["AR"]))
    check("empty name gives empty acronym", derive_short("") == "")


def test_new_speaker_defaults():
    roster = []
    for i in range(3):
        roster.append(new_speaker(i, roster=roster))
    check("default names are numbered",
          [s["name"] for s in roster] == ["Speaker 1", "Speaker 2", "Speaker 3"],
          str([s["name"] for s in roster]))
    check("ids are unique", len({s["id"] for s in roster}) == 3)
    check("colours come from the palette in order",
          [s["color"] for s in roster] == list(PALETTE[:3]))
    check("acronyms are unique", len({s["short"] for s in roster}) == 3,
          str([s["short"] for s in roster]))


def test_turn_is_a_maximal_same_speaker_run():
    doc = doc_with([
        seg(0, 0.0, 4.0, "So tell me about the programme.", "s1"),
        seg(1, 4.2, 9.0, "It started last spring.", "s2"),
        seg(2, 9.1, 13.0, "We had three classes involved.", "s2"),
        seg(3, 13.2, 15.0, "And how did that go?", "s1"),
    ], two_speakers())
    normalise(doc)
    groups = turns(doc)

    check("consecutive same-speaker segments form one turn",
          [len(t["segments"]) for t in groups] == [1, 2, 1],
          str([len(t["segments"]) for t in groups]))
    check("turns alternate speakers",
          [t["speaker_id"] for t in groups] == ["s1", "s2", "s1"],
          str([t["speaker_id"] for t in groups]))
    check("no two consecutive turns share a speaker",
          all(a["speaker_id"] != b["speaker_id"] for a, b in zip(groups, groups[1:])))


def test_pause_inside_a_turn_does_not_start_a_new_turn():
    """The core requirement: a long pause must not reprint the name."""
    doc = doc_with([
        seg(0, 0.0, 5.0, "It started last spring, I think.", "s2"),
        # 6.5 second pause, same speaker still talking
        seg(1, 11.5, 16.0, "Sorry, actually it was the autumn term.", "s2"),
    ], two_speakers())
    normalise(doc)
    groups = turns(doc, gap=1.5)

    check("a pause by the same speaker stays one turn", len(groups) == 1,
          f"got {len(groups)} turns")
    check("the pause is recorded, not used as a boundary",
          len(groups[0]["pauses"]) == 1, str(groups[0]["pauses"]))
    # normalise() recomputes a segment's end from its word timings, so the
    # real gap is derived rather than assumed.
    expected = doc["segments"][1]["start"] - doc["segments"][0]["end"]
    check("the pause length is reported",
          abs(groups[0]["pauses"][0]["seconds"] - expected) < 0.02,
          f'got {groups[0]["pauses"]}, expected ~{expected:.2f}')

    blocks = turn_blocks(groups[0])
    check("the turn splits into two paragraph blocks", len(blocks) == 2,
          f"got {len(blocks)}")
    check("the first block has no leading pause", blocks[0]["pause_before"] is None)
    check("the second block carries the pause",
          blocks[1]["pause_before"] is not None
          and abs(blocks[1]["pause_before"] - expected) < 0.02,
          f'got {blocks[1]["pause_before"]}, expected ~{expected:.2f}')


def test_name_printed_once_per_turn():
    """Simulate a renderer: names emitted per turn, never within one."""
    doc = doc_with([
        seg(0, 0.0, 4.0, "Tell me about it.", "s1"),
        seg(1, 5.0, 9.0, "Well, it began in March.", "s2"),
        seg(2, 16.0, 20.0, "Or maybe April.", "s2"),   # 7s pause, same speaker
        seg(3, 21.0, 24.0, "Right.", "s1"),
    ], two_speakers())
    normalise(doc)

    emitted = []
    for turn in turns(doc, gap=1.5):
        speaker = T.speaker_by_id(doc, turn["speaker_id"])
        emitted.append(speaker["name"] if speaker else "")

    check("one name per turn, in order",
          emitted == ["Interviewer", "Participant 04", "Interviewer"],
          str(emitted))
    check("the paused speaker's name appears only once",
          emitted.count("Participant 04") == 1, str(emitted))


def test_untagged_segments_group_at_pauses():
    doc = doc_with([
        seg(0, 0.0, 4.0, "First thought here."),
        seg(1, 4.1, 8.0, "Continues straight on."),
        seg(2, 14.0, 18.0, "New thought after a long gap."),
    ])
    normalise(doc)
    groups = turns(doc, gap=1.5)
    check("untagged text splits at pauses into clickable units",
          [len(t["segments"]) for t in groups] == [2, 1],
          str([len(t["segments"]) for t in groups]))
    check("untagged turns have no speaker",
          all(t["speaker_id"] == "" for t in groups))


def test_assign_speaker_tags_a_whole_turn():
    doc = doc_with([
        seg(0, 0.0, 4.0, "One."),
        seg(1, 4.1, 8.0, "Two."),
        seg(2, 14.0, 18.0, "Three."),
    ], two_speakers())
    normalise(doc)
    changed = assign_speaker(doc, [0, 1], "s2")
    check("assigning a turn updates every segment in it", changed == 2, str(changed))
    check("the display name is resolved from the roster",
          [s["speaker"] for s in doc["segments"]] == ["Participant 04", "Participant 04", ""],
          str([s["speaker"] for s in doc["segments"]]))
    check("assigning marks the document as human edited", doc["human_edited"])

    check("assigning an unknown speaker is refused",
          _raises(lambda: assign_speaker(doc, [2], "nope")))

    assign_speaker(doc, [0], "")
    check("an empty id clears the tag", doc["segments"][0]["speaker"] == "",
          repr(doc["segments"][0]["speaker"]))


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


def test_legacy_transcript_migrates():
    """A schema-2 document with free-text speakers keeps its tagging."""
    legacy = {
        "schema": 2,
        "duration": 60.0,
        "model": "large-v3-turbo",
        "segments": [
            dict(seg(0, 0.0, 4.0, "Question one."), speaker="Interviewer"),
            dict(seg(1, 4.5, 9.0, "An answer."), speaker="P01"),
            dict(seg(2, 9.5, 12.0, "And another."), speaker="P01"),
        ],
    }
    for s in legacy["segments"]:
        s.pop("speaker_id", None)

    normalise(legacy)

    check("a roster is built from the old strings",
          [s["name"] for s in legacy["speakers"]] == ["Interviewer", "P01"],
          str(legacy.get("speakers")))
    check("segments gain matching speaker ids",
          [s["speaker_id"] for s in legacy["segments"]] == ["s1", "s2", "s2"],
          str([s["speaker_id"] for s in legacy["segments"]]))
    check("display names survive migration",
          [s["speaker"] for s in legacy["segments"]] == ["Interviewer", "P01", "P01"])
    check("the schema is upgraded", legacy["schema"] == T.SCHEMA_VERSION)
    check("turns respect the migrated tags",
          [len(t["segments"]) for t in turns(legacy)] == [1, 2],
          str([len(t["segments"]) for t in turns(legacy)]))

    before = [s["speaker_id"] for s in legacy["segments"]]
    migrate_speakers(legacy)
    check("migration is idempotent",
          [s["speaker_id"] for s in legacy["segments"]] == before)


def test_removing_a_speaker_unassigns_rather_than_orphans():
    doc = doc_with([
        seg(0, 0.0, 4.0, "One.", "s1"),
        seg(1, 5.0, 9.0, "Two.", "s2"),
    ], two_speakers())
    normalise(doc)

    report = set_roster(doc, [two_speakers()[0]])
    check("removal is reported", report["unassigned_segments"] == 1, str(report))
    check("the removed speaker is named", report["removed_speakers"] == ["Participant 04"],
          str(report))
    check("the orphaned segment is cleared, not left dangling",
          doc["segments"][1]["speaker_id"] == "" and doc["segments"][1]["speaker"] == "",
          str(doc["segments"][1]))
    check("the surviving speaker is untouched",
          doc["segments"][0]["speaker"] == "Interviewer")


def test_clean_roster_repairs_input():
    cleaned = clean_roster([
        {"name": "  Ana Ruiz  "},
        {"id": "s1", "name": "", "color": "not-a-colour"},
        {"id": "s1", "name": "Duplicate id", "short": "DUP"},
        "not a dict",
    ])
    check("bad entries are dropped", len(cleaned) == 3, str(len(cleaned)))
    check("names are trimmed", cleaned[0]["name"] == "Ana Ruiz", cleaned[0]["name"])
    check("missing names are numbered", cleaned[1]["name"] == "Speaker 2",
          cleaned[1]["name"])
    check("invalid colours fall back to the palette",
          cleaned[1]["color"] == PALETTE[1], cleaned[1]["color"])
    check("duplicate ids are made unique",
          len({s["id"] for s in cleaned}) == 3, str([s["id"] for s in cleaned]))
    check("acronyms are filled in", all(s["short"] for s in cleaned),
          str([s["short"] for s in cleaned]))


def test_paragraphs_still_works_for_legacy_callers():
    doc = doc_with([
        seg(0, 0.0, 4.0, "One.", "s1"),
        seg(1, 12.0, 16.0, "Two, after a pause.", "s1"),
        seg(2, 17.0, 20.0, "Three.", "s2"),
    ], two_speakers())
    normalise(doc)
    blocks = T.paragraphs(doc, gap=1.5)
    check("paragraphs still splits on pause and speaker", len(blocks) == 3,
          str(len(blocks)))


def main() -> int:
    tests = [
        test_derive_short,
        test_new_speaker_defaults,
        test_turn_is_a_maximal_same_speaker_run,
        test_pause_inside_a_turn_does_not_start_a_new_turn,
        test_name_printed_once_per_turn,
        test_untagged_segments_group_at_pauses,
        test_assign_speaker_tags_a_whole_turn,
        test_legacy_transcript_migrates,
        test_removing_a_speaker_unassigns_rather_than_orphans,
        test_clean_roster_repairs_input,
        test_paragraphs_still_works_for_legacy_callers,
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
