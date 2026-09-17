"""Re-timestamping: repair word timings after a human edits the transcript.

The problem
-----------
Whisper produces per-word timings. A human then corrects the text -- fixing a
misheard name, deleting a false start, inserting a word the model dropped. The
text is now right and the timings are stale, so the caption files no longer
line up with the audio.

The approach
------------
Diff the edited word sequence against the original *timed* word sequence with
``difflib.SequenceMatcher`` and transfer timings across the alignment:

* **equal**     -- words unchanged, keep their exact original timings.
* **replace**   -- distribute the original span across the new words in
                   proportion to their character length, which tracks speaking
                   time far better than an equal split.
* **insert**    -- interpolate inside the gap between the surrounding anchors.
* **delete**    -- the span collapses; neighbours absorb it.

This is deterministic, runs in milliseconds, needs no model and no network,
and -- crucially -- leaves untouched text bit-for-bit identical in timing. A
forced-alignment model would be more accurate on heavily rewritten passages,
but it would also need another gigabyte of weights and would perturb timings
the human never questioned.

Matching is done on a normalised key (lowercased, punctuation stripped) so that
correcting "teh" to "the" or adding a comma still counts as the same word and
keeps its timing.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .transcript import _now, make_word, normalise, segment_text

_TOKEN = re.compile(r"\S+")
_STRIP = re.compile(r"[^\w']+", re.UNICODE)

# A replaced run longer than this many words is reported separately, because
# wholesale rewrites are where proportional distribution is weakest and the
# operator deserves to know which passages to spot-check.
LARGE_REWRITE = 8


def _key(token: str) -> str:
    return _STRIP.sub("", token.lower())


def tokenise(text: str) -> list:
    return _TOKEN.findall(text or "")


def realign_segment(seg: dict, *, fallback_start: float, fallback_end: float) -> dict:
    """Return realignment stats for one segment, rebuilding its word list."""
    new_tokens = tokenise(segment_text(seg))
    old_words = list(seg.get("words") or [])

    stats = {
        "preserved": 0,
        "redistributed": 0,
        "interpolated": 0,
        "dropped": 0,
        "large_rewrites": 0,
    }

    if not new_tokens:
        seg["words"] = []
        seg["start"] = round(float(fallback_start), 3)
        seg["end"] = round(max(float(fallback_start) + 0.01, float(fallback_end)), 3)
        seg.pop("stale_timings", None)
        return stats

    # No timed source to work from: spread the segment span evenly by length.
    if not old_words:
        span_start = float(seg.get("start") if seg.get("start") is not None else fallback_start)
        span_end = float(seg.get("end") if seg.get("end") is not None else fallback_end)
        if span_end <= span_start:
            span_end = span_start + max(0.3, 0.28 * len(new_tokens))
        seg["words"] = _spread(new_tokens, span_start, span_end, prob=0.0)
        stats["interpolated"] = len(new_tokens)
        seg["start"] = seg["words"][0]["start"]
        seg["end"] = seg["words"][-1]["end"]
        seg.pop("stale_timings", None)
        return stats

    old_keys = [_key(w.get("w", "")) for w in old_words]
    new_keys = [_key(t) for t in new_tokens]

    matcher = SequenceMatcher(a=old_keys, b=new_keys, autojunk=False)
    rebuilt: list = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                source = old_words[i1 + offset]
                rebuilt.append(make_word(
                    new_tokens[j1 + offset],
                    source["start"], source["end"],
                    source.get("prob", 1.0),
                ))
            stats["preserved"] += i2 - i1

        elif tag == "replace":
            span_start, span_end = _span(old_words, i1, i2, rebuilt, fallback_end)
            tokens = new_tokens[j1:j2]
            # Rewritten words get confidence 0: a human-corrected word has no
            # model confidence, and inheriting the replaced word's score would
            # mislead the editor's low-confidence highlighting.
            rebuilt.extend(_spread(tokens, span_start, span_end, prob=0.0))
            stats["redistributed"] += len(tokens)
            if len(tokens) > LARGE_REWRITE or (i2 - i1) > LARGE_REWRITE:
                stats["large_rewrites"] += 1

        elif tag == "insert":
            tokens = new_tokens[j1:j2]
            # Anchor the insertion between the previous rebuilt word and the
            # next surviving original word.
            prev_end = rebuilt[-1]["end"] if rebuilt else float(
                old_words[i1]["start"] if i1 < len(old_words) else fallback_start
            )
            next_start = float(
                old_words[i1]["start"] if i1 < len(old_words) else fallback_end
            )
            if next_start <= prev_end:
                # No room between anchors: borrow a nominal slice. Downstream
                # normalisation keeps everything monotonic.
                next_start = prev_end + max(0.12 * len(tokens), 0.12)
            rebuilt.extend(_spread(tokens, prev_end, next_start, prob=0.0))
            stats["interpolated"] += len(tokens)

        elif tag == "delete":
            stats["dropped"] += i2 - i1

    if not rebuilt:
        rebuilt = _spread(new_tokens, fallback_start, fallback_end, prob=0.0)
        stats["interpolated"] += len(new_tokens)

    seg["words"] = rebuilt
    seg["start"] = rebuilt[0]["start"]
    seg["end"] = rebuilt[-1]["end"]
    seg.pop("stale_timings", None)
    return stats


def _span(old_words: list, i1: int, i2: int, rebuilt: list, fallback_end: float):
    """Time span the replaced original words occupied."""
    start = float(old_words[i1]["start"])
    end = float(old_words[i2 - 1]["end"])
    if rebuilt and start < rebuilt[-1]["end"]:
        start = rebuilt[-1]["end"]
    if end <= start:
        end = start + 0.2
    return start, min(end, max(end, float(fallback_end)))


def _spread(tokens: list, start: float, end: float, prob: float = 0.0) -> list:
    """Distribute tokens across a span in proportion to character length."""
    start = float(start)
    end = float(end)
    if end <= start:
        end = start + max(0.15 * len(tokens), 0.15)

    weights = [max(1, len(_key(t)) or len(t)) for t in tokens]
    total = float(sum(weights)) or 1.0
    available = end - start

    out = []
    cursor = start
    for token, weight in zip(tokens, weights):
        share = available * (weight / total)
        word_end = cursor + share
        out.append(make_word(token, cursor, word_end, prob))
        cursor = word_end
    if out:
        out[-1]["end"] = round(end, 3)
    return out


def retimestamp(doc: dict) -> dict:
    """Re-derive word timings for the whole document from its edited text.

    Returns a report suitable for the console: how many words kept their exact
    original timing, how many were redistributed or interpolated, and which
    segments contained large rewrites worth spot-checking.
    """
    segments = doc.get("segments") or []
    duration = float(doc.get("duration") or 0.0)

    totals = {
        "segments": len(segments),
        "segments_changed": 0,
        "preserved": 0,
        "redistributed": 0,
        "interpolated": 0,
        "dropped": 0,
        "large_rewrites": 0,
        "review_segments": [],
    }

    for index, seg in enumerate(segments):
        previous_end = float(segments[index - 1].get("end") or 0.0) if index else 0.0
        if index + 1 < len(segments):
            next_start = float(segments[index + 1].get("start") or duration or 0.0)
        else:
            next_start = duration or float(seg.get("end") or previous_end)

        before = [(w.get("w"), w.get("start"), w.get("end")) for w in (seg.get("words") or [])]

        stats = realign_segment(
            seg,
            fallback_start=max(previous_end, float(seg.get("start") or previous_end)),
            fallback_end=max(next_start, previous_end + 0.2),
        )

        after = [(w.get("w"), w.get("start"), w.get("end")) for w in (seg.get("words") or [])]
        if before != after:
            totals["segments_changed"] += 1

        for key in ("preserved", "redistributed", "interpolated", "dropped", "large_rewrites"):
            totals[key] += stats[key]
        if stats["large_rewrites"]:
            totals["review_segments"].append(int(seg.get("id", index)))

    normalise(doc)
    doc["retimestamped_at"] = _now()

    total_words = totals["preserved"] + totals["redistributed"] + totals["interpolated"]
    totals["words"] = total_words
    totals["exact_pct"] = round(
        100.0 * totals["preserved"] / total_words, 1
    ) if total_words else 0.0
    return totals


def describe_report(report: dict) -> str:
    """One-line console summary of a re-timestamp pass."""
    return (
        f"{report['words']} words: {report['preserved']} kept exact timings "
        f"({report['exact_pct']}%), {report['redistributed']} redistributed, "
        f"{report['interpolated']} interpolated, {report['dropped']} removed "
        f"across {report['segments_changed']}/{report['segments']} segments"
    )
