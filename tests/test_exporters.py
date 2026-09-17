"""Tests for caption building and the export writers.

Run with:  .venv/Scripts/python.exe -m tests.test_exporters
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import default_settings
from app.exporters import (
    build_cues,
    export_all,
    fits,
    srt_time,
    stamp,
    vtt_time,
    wrap_lines,
)
from app.transcript import apply_edits, make_segment, make_word, new_document

FAILURES: list = []

VTT_CUE_TIME = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3}) --> (\d{2}):(\d{2}):(\d{2})\.(\d{3})$"
)


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def build_doc(speaker_a="Interviewer", speaker_b="P01"):
    text = (
        "Thank you for agreeing to take part in this interview today, I really "
        "appreciate it. I want to start by asking about your experience with "
        "the programme and how you first came to hear about it at all."
    )
    tokens = text.split()
    words = []
    cursor = 0.0
    for token in tokens:
        words.append(make_word(token, cursor, cursor + 0.30, 0.95))
        cursor += 0.32

    half = len(words) // 2
    doc = new_document(duration=cursor + 1, model="large-v3-turbo",
                       language="en", language_probability=0.98)
    doc["segments"] = [
        make_segment(0, words[0]["start"], words[half - 1]["end"],
                     " ".join(w["w"] for w in words[:half]), words[:half],
                     speaker=speaker_a),
        make_segment(1, words[half]["start"], words[-1]["end"],
                     " ".join(w["w"] for w in words[half:]), words[half:],
                     speaker=speaker_b),
    ]
    return doc


def parse_vtt(path: Path) -> list:
    """Minimal WebVTT parser that also validates structure."""
    lines = path.read_text(encoding="utf-8").splitlines()
    check("VTT starts with the WEBVTT signature", lines and lines[0] == "WEBVTT",
          f"got {lines[0]!r}" if lines else "empty file")

    cues = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = VTT_CUE_TIME.match(line)
        if match:
            payload = []
            index += 1
            while index < len(lines) and lines[index].strip():
                payload.append(lines[index])
                index += 1
            cues.append({"timing": line, "lines": payload})
        else:
            index += 1
    return cues


def to_seconds(clock: str) -> float:
    hours, minutes, rest = clock.split(":")
    secs, ms = rest.replace(",", ".").split(".")
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(ms) / 1000.0


def test_time_formats():
    check("vtt time uses a dot", vtt_time(3725.4567) == "01:02:05.457",
          vtt_time(3725.4567))
    check("srt time uses a comma", srt_time(3725.4567) == "01:02:05,457",
          srt_time(3725.4567))
    check("negative time clamps to zero", vtt_time(-5) == "00:00:00.000", vtt_time(-5))
    check("hour rollover is correct", vtt_time(7200) == "02:00:00.000", vtt_time(7200))
    check("readable stamp", stamp(3725.9) == "01:02:06", stamp(3725.9))


def test_wrap_respects_width():
    lines = wrap_lines("one two three four five six seven eight nine ten", 20)
    check("every wrapped line is within the width",
          all(len(line) <= 20 for line in lines), f"got {lines}")
    check("wrapping preserves the words",
          " ".join(lines) == "one two three four five six seven eight nine ten",
          f"got {lines}")
    long_word = wrap_lines("short supercalifragilisticexpialidocious", 10)
    check("an overlong word is not split",
          "supercalifragilisticexpialidocious" in long_word, f"got {long_word}")


def test_fits_accounts_for_greedy_wrap():
    # 84 characters do not fit two 42-char lines under greedy wrapping; the
    # naive chars x lines budget would wrongly accept this.
    text = ("I want to start by asking about your experience with the "
            "programme and how you first")
    check("fits() rejects text that greedy wrap cannot fit",
          not fits(text, 42, 2), f"len={len(text)}")
    check("fits() accepts genuinely short text", fits("hello there", 42, 2))
    # Same text, same limit: it fits on one line alone but not once a
    # 10-character speaker tag is rendered ahead of it.
    check("fits() accepts text that fits without a prefix",
          fits("aaaa bbbb cccc", 20, 1))
    check("fits() rejects the same text once a prefix is reserved",
          not fits("aaaa bbbb cccc", 20, 1, reserve=10))


def test_cues_respect_limits():
    doc = build_doc()
    settings = default_settings()
    cues = build_cues(doc, 42, 2, 6.0)
    check("cues were produced", len(cues) > 1, f"got {len(cues)}")

    for cue in cues:
        body = cue["text"]
        if cue["speaker"]:
            body = f"<v {cue['speaker']}>{body}"
        lines = wrap_lines(body, 42)
        check(f"cue at {cue['start']:.2f}s wraps to <= 2 lines",
              len(lines) <= 2, f"got {len(lines)}: {lines}")
        check(f"cue at {cue['start']:.2f}s is <= 6s",
              cue["end"] - cue["start"] <= 6.01,
              f"got {cue['end'] - cue['start']:.2f}s")
        check(f"cue at {cue['start']:.2f}s is non-empty", bool(cue["text"].strip()))


def test_cues_are_monotonic_and_cover_words():
    doc = build_doc()
    cues = build_cues(doc, 42, 2, 6.0)
    for a, b in zip(cues, cues[1:]):
        check(f"cue {a['start']:.2f} precedes {b['start']:.2f}",
              a["end"] <= b["start"] + 1e-6,
              f"{a['end']} > {b['start']}")

    spoken = " ".join(
        w["w"] for seg in doc["segments"] for w in seg["words"]
    )
    captioned = " ".join(c["text"] for c in cues)
    check("captions contain every spoken word", spoken == captioned,
          f"\n   spoken   ={spoken[:80]}\n   captioned={captioned[:80]}")


def test_vtt_is_wellformed():
    doc = build_doc()
    settings = default_settings()
    out = Path(tempfile.mkdtemp()) / "outputs"
    result = export_all(doc, out, settings, {"title": "Interview 01"})

    for fmt, value in result.items():
        check(f"{fmt} wrote without error", not str(value).startswith("error"), str(value))

    cues = parse_vtt(Path(result["vtt"]))
    check("VTT cue count matches the builder",
          len(cues) == len(build_cues(doc, 42, 2, 6.0)),
          f"file={len(cues)} builder={len(build_cues(doc, 42, 2, 6.0))}")

    previous_end = -1.0
    for cue in cues:
        start_s, end_s = cue["timing"].split(" --> ")
        start, end = to_seconds(start_s), to_seconds(end_s)
        check(f"VTT cue {start_s} ends after it starts", end > start,
              f"{start} -> {end}")
        check(f"VTT cue {start_s} follows the previous cue", start >= previous_end - 1e-6,
              f"{start} < {previous_end}")
        previous_end = end
        check(f"VTT cue {start_s} has payload", bool(cue["lines"]))


def test_segment_without_words_still_captioned():
    doc = build_doc()
    apply_edits(doc, [{"id": 0, "text": "A hand-typed replacement line."}])
    # Deliberately NOT re-timestamped: words are stale/absent for that segment.
    doc["segments"][0]["words"] = []
    cues = build_cues(doc, 42, 2, 6.0)
    joined = " ".join(c["text"] for c in cues)
    check("an un-retimestamped segment is not dropped from captions",
          "hand-typed replacement" in joined, f"got {joined[:100]}")


def test_exports_reflect_edits():
    doc = build_doc()
    apply_edits(doc, [{"id": 1, "speaker": "Participant 01"}])
    settings = default_settings()
    out = Path(tempfile.mkdtemp()) / "outputs"
    result = export_all(doc, out, settings, {"title": "T", "filename": "a.mp4"})

    txt = Path(result["txt"]).read_text(encoding="utf-8")
    check("txt carries the new speaker label", "Participant 01:" in txt)
    md = Path(result["md"]).read_text(encoding="utf-8")
    check("md front matter lists the speaker with its acronym",
          '- name: "Participant 01"' in md and "short:" in md,
          md[:400])
    check("md declares local processing", "No audio or text left this machine" in md)
    vtt = Path(result["vtt"]).read_text(encoding="utf-8")
    # Captions default to the acronym, because a full pseudonym eats the very
    # limited room a caption line has.
    check("vtt carries the speaker voice tag as an acronym",
          "<v P01>" in vtt, vtt[:300])

    full = dict(settings)
    full["caption_speaker_style"] = "name"
    out2 = Path(tempfile.mkdtemp()) / "outputs"
    result2 = export_all(doc, out2, full, {"title": "T"}, formats=["vtt"])
    check("captions can use the full name instead",
          "<v Participant 01>" in Path(result2["vtt"]).read_text(encoding="utf-8"))
    check("docx is a non-trivial file", Path(result["docx"]).stat().st_size > 10_000,
          str(Path(result["docx"]).stat().st_size))


def test_speaker_name_not_repeated_after_a_pause():
    """The defect this feature set fixes.

    One person talking either side of a long silence is still one turn, so
    their name must be printed once. Before the turn model, every exporter
    started a new paragraph at the pause and reprinted the name.
    """
    from app.transcript import make_segment, make_word, new_document, normalise

    def build(index, start, end, text, speaker_id):
        words, cursor = [], start
        span = (end - start) / max(1, len(text.split()))
        for token in text.split():
            words.append(make_word(token, cursor, cursor + span * 0.9, 0.97))
            cursor += span
        return make_segment(index, start, end, text, words, speaker_id=speaker_id)

    doc = new_document(duration=60.0, model="test", language="en")
    doc["speakers"] = [
        {"id": "s1", "name": "Interviewer", "short": "INT", "color": "#0f766e"},
        {"id": "s2", "name": "Participant 04", "short": "P04", "color": "#b45309"},
    ]
    doc["segments"] = [
        build(0, 0.0, 4.0, "So how did the pilot go?", "s1"),
        build(1, 5.0, 10.0, "It went well overall I think.", "s2"),
        # Six second silence, same speaker resumes.
        build(2, 16.0, 21.0, "Although the second week was harder.", "s2"),
        build(3, 22.0, 25.0, "Harder how?", "s1"),
    ]
    normalise(doc)

    settings = default_settings()
    out = Path(tempfile.mkdtemp()) / "outputs"
    result = export_all(doc, out, settings, {"title": "Pilot"},
                        formats=["txt", "md"])

    txt = Path(result["txt"]).read_text(encoding="utf-8")
    body = txt.split("Review status:", 1)[-1]
    check("the paused speaker is named exactly once in .txt",
          body.count("Participant 04:") == 1,
          f'counted {body.count("Participant 04:")}')
    check("the other speaker is named for each of their turns",
          body.count("Interviewer:") == 2, f'counted {body.count("Interviewer:")}')
    check("the pause itself is marked", "pause)" in body, body[:400])

    md = Path(result["md"]).read_text(encoding="utf-8")
    md_body = md.split("## Transcript", 1)[-1]
    check("the paused speaker is named exactly once in .md",
          md_body.count("**Participant 04**") == 1,
          f'counted {md_body.count("**Participant 04**")}')

    # And the text either side of the pause survives.
    check("text before the pause is present", "went well overall" in txt)
    check("text after the pause is present", "second week was harder" in txt)


def test_pause_markers_can_be_switched_off():
    from app.transcript import make_segment, make_word, new_document, normalise

    doc = new_document(duration=60.0, model="test", language="en")
    doc["speakers"] = [{"id": "s1", "name": "A", "short": "A", "color": "#0f766e"}]
    doc["segments"] = [
        make_segment(0, 0.0, 3.0, "First part here.",
                     [make_word("First", 0.0, 1.0), make_word("part", 1.0, 2.0),
                      make_word("here.", 2.0, 3.0)], speaker_id="s1"),
        make_segment(1, 12.0, 15.0, "Second part here.",
                     [make_word("Second", 12.0, 13.0), make_word("part", 13.0, 14.0),
                      make_word("here.", 14.0, 15.0)], speaker_id="s1"),
    ]
    normalise(doc)

    settings = default_settings()
    settings["show_pause_markers"] = False
    out = Path(tempfile.mkdtemp()) / "outputs"
    result = export_all(doc, out, settings, {"title": "T"}, formats=["txt"])
    txt = Path(result["txt"]).read_text(encoding="utf-8")
    check("pause markers can be disabled", "pause)" not in txt, txt[-200:])
    check("the name is still printed only once",
          txt.count("A:") == 1, f'counted {txt.count("A:")}')


def main() -> int:
    tests = [
        test_speaker_name_not_repeated_after_a_pause,
        test_pause_markers_can_be_switched_off,
        test_time_formats,
        test_wrap_respects_width,
        test_fits_accounts_for_greedy_wrap,
        test_cues_respect_limits,
        test_cues_are_monotonic_and_cover_words,
        test_vtt_is_wellformed,
        test_segment_without_words_still_captioned,
        test_exports_reflect_edits,
    ]
    for test in tests:
        print(f"\n{test.__name__}")
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {sorted(set(FAILURES))}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
