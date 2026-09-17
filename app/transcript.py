"""The canonical transcript document.

``transcript.json`` is the single source of truth for a document. Every export
(.txt, .md, .docx, .vtt, .srt) is a pure projection of it, and the editor reads
and writes it directly. Keeping one authoritative structure is what makes the
re-timestamp pass and the caption files agree with each other.

Shape
-----
::

    {
      "schema": 2,
      "created": "...", "updated": "...",
      "language": "en", "language_probability": 0.99,
      "duration": 3612.4,
      "model": "large-v3-turbo",
      "engine": {...},          # settings actually used, for reproducibility
      "stats": {...},           # throughput measured during transcription
      "human_edited": false,
      "segments": [
        {"id": 0, "start": 0.0, "end": 4.2, "text": "...",
         "speaker": "", "edited": false,
         "words": [{"w": "Hello", "start": 0.1, "end": 0.4, "prob": 0.98}],
         "avg_logprob": -0.21, "no_speech_prob": 0.01,
         "compression_ratio": 1.4, "temperature": 0.0}
      ]
    }
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

SCHEMA_VERSION = 2

# Confidence below which the editor tints a word for attention.
LOW_CONFIDENCE = 0.55

_WORD_SPLIT = re.compile(r"\S+")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_document(
    *,
    duration: float,
    model: str,
    language: str = "",
    language_probability: float = 0.0,
    engine: dict | None = None,
) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "created": _now(),
        "updated": _now(),
        "language": language,
        "language_probability": round(float(language_probability or 0.0), 4),
        "duration": round(float(duration or 0.0), 3),
        "model": model,
        "engine": engine or {},
        "stats": {},
        "human_edited": False,
        "retimestamped_at": None,
        "segments": [],
    }


def make_word(text: str, start: float, end: float, prob: float = 1.0) -> dict:
    # Whisper emits words with their leading space attached ("The ", "quick ").
    # The bare token is stored instead, because the editor renders one span per
    # word and the authoritative prose lives in the segment's `text` field.
    return {
        "w": text.strip(),
        "start": round(float(start), 3),
        "end": round(float(end), 3),
        "prob": round(float(prob), 4),
    }


def make_segment(
    index: int,
    start: float,
    end: float,
    text: str,
    words: list | None = None,
    **meta,
) -> dict:
    seg = {
        "id": index,
        "start": round(float(start), 3),
        "end": round(float(end), 3),
        "text": text.strip(),
        "speaker": meta.get("speaker", ""),
        "edited": bool(meta.get("edited", False)),
        "words": words or [],
        "avg_logprob": meta.get("avg_logprob"),
        "no_speech_prob": meta.get("no_speech_prob"),
        "compression_ratio": meta.get("compression_ratio"),
        "temperature": meta.get("temperature"),
    }
    return seg


def from_engine_segment(index: int, seg) -> dict:
    """Convert one faster-whisper Segment into our canonical form."""
    words = []
    for w in (getattr(seg, "words", None) or []):
        words.append(make_word(w.word, w.start, w.end, getattr(w, "probability", 1.0)))
    return make_segment(
        index,
        seg.start,
        seg.end,
        seg.text,
        words,
        avg_logprob=_round_or_none(getattr(seg, "avg_logprob", None), 4),
        no_speech_prob=_round_or_none(getattr(seg, "no_speech_prob", None), 4),
        compression_ratio=_round_or_none(getattr(seg, "compression_ratio", None), 3),
        temperature=_round_or_none(getattr(seg, "temperature", None), 2),
    )


def _round_or_none(value, digits):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Derived views
# ---------------------------------------------------------------------------


def segment_text(seg: dict) -> str:
    """Text of a segment.

    ``text`` is authoritative and ``words`` carry timing only. That ordering
    matters: after a human edit the text is new while the words are still the
    model's, so preferring the word join would silently discard the edit. The
    re-timestamp pass is what brings words back into agreement with text.
    """
    text = (seg.get("text") or "").strip()
    if text:
        return text
    # Fallback only: `text` is normally authoritative. Space-joining is
    # right for space-delimited languages and harmless here because this
    # path is reached only when text is missing entirely.
    return " ".join(w.get("w", "") for w in (seg.get("words") or [])).strip()


def full_text(doc: dict) -> str:
    return " ".join(
        t for t in (segment_text(s) for s in doc.get("segments", [])) if t
    ).strip()


def word_count(doc: dict) -> int:
    # Counted from text, not from the word list, so the number stays correct
    # after a human edit adds or removes words but before a re-timestamp.
    return sum(
        len(_WORD_SPLIT.findall(segment_text(seg)))
        for seg in doc.get("segments", [])
    )


def _prob_of(word: dict) -> float:
    """Confidence of a word, treating 0.0 as a real value rather than absent.

    A human-corrected word is stored with confidence 0.0, so the usual
    ``word.get("prob") or 1.0`` idiom would silently promote it to full
    confidence and hide it from the editor's low-confidence highlighting.
    """
    value = word.get("prob")
    if value is None:
        return 1.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1.0


def all_words(doc: dict) -> list:
    """Flat list of (segment_index, word_index, word) across the document."""
    out = []
    for si, seg in enumerate(doc.get("segments", [])):
        for wi, word in enumerate(seg.get("words") or []):
            out.append((si, wi, word))
    return out


def low_confidence_count(doc: dict, threshold: float = LOW_CONFIDENCE) -> int:
    return sum(
        1 for _si, _wi, w in all_words(doc)
        if _prob_of(w) < threshold
    )


def speakers(doc: dict) -> list:
    seen = []
    for seg in doc.get("segments", []):
        name = (seg.get("speaker") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def summary(doc: dict) -> dict:
    segs = doc.get("segments", [])
    return {
        "segments": len(segs),
        "words": word_count(doc),
        "duration": doc.get("duration") or 0.0,
        "language": doc.get("language") or "",
        "model": doc.get("model") or "",
        "human_edited": bool(doc.get("human_edited")),
        "retimestamped_at": doc.get("retimestamped_at"),
        "low_confidence": low_confidence_count(doc),
        "speakers": speakers(doc),
        "edited_segments": sum(1 for s in segs if s.get("edited")),
    }


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalise(doc: dict) -> dict:
    """Repair ordering, ids, and monotonicity in place, then return the doc.

    Called after every edit and every re-timestamp so that downstream code --
    caption splitting in particular -- can assume sane, non-overlapping,
    increasing timings.
    """
    segments = doc.get("segments") or []
    duration = float(doc.get("duration") or 0.0)

    segments.sort(key=lambda s: (float(s.get("start") or 0.0), float(s.get("end") or 0.0)))

    previous_end = 0.0
    for index, seg in enumerate(segments):
        seg["id"] = index

        words = seg.get("words") or []
        cleaned = []
        for word in words:
            text = (word.get("w") or "")
            if not text.strip():
                continue
            start = float(word.get("start") or 0.0)
            end = float(word.get("end") or start)
            if end < start:
                end = start
            cleaned.append({
                "w": text,
                "start": round(start, 3),
                "end": round(end, 3),
                "prob": round(_prob_of(word), 4),
            })
        cleaned.sort(key=lambda w: (w["start"], w["end"]))

        # Enforce monotonic, non-overlapping word timings.
        cursor = 0.0
        for word in cleaned:
            if word["start"] < cursor:
                word["start"] = round(cursor, 3)
            if word["end"] <= word["start"]:
                word["end"] = round(word["start"] + 0.01, 3)
            cursor = word["end"]
        seg["words"] = cleaned

        if cleaned:
            seg["start"] = cleaned[0]["start"]
            seg["end"] = cleaned[-1]["end"]
        else:
            seg["start"] = round(max(previous_end, float(seg.get("start") or 0.0)), 3)
            seg["end"] = round(max(seg["start"] + 0.01, float(seg.get("end") or 0.0)), 3)

        if seg["start"] < previous_end:
            seg["start"] = round(previous_end, 3)
        if seg["end"] <= seg["start"]:
            seg["end"] = round(seg["start"] + 0.01, 3)
        previous_end = seg["end"]

        # Only backfill text from words when text is missing; never overwrite,
        # or a human edit would be lost the next time the doc is normalised.
        if not (seg.get("text") or "").strip():
            seg["text"] = " ".join(w["w"] for w in cleaned).strip()
        seg["speaker"] = (seg.get("speaker") or "").strip()

    if duration and previous_end > duration:
        doc["duration"] = round(previous_end, 3)

    doc["segments"] = segments
    doc["updated"] = _now()
    return doc


# ---------------------------------------------------------------------------
# Edit application
# ---------------------------------------------------------------------------


def apply_edits(doc: dict, edits: list) -> dict:
    """Apply editor changes to the document.

    ``edits`` is a list of ``{"id": n, "text": "...", "speaker": "..."}``. Text
    changes deliberately do NOT touch word timings here: the words are left as
    the model produced them and the transcript is marked as needing a
    re-timestamp. That keeps the destructive step explicit and user-triggered
    rather than silently guessing timings on every keystroke.
    """
    by_id = {int(s["id"]): s for s in doc.get("segments", [])}
    touched = 0

    for edit in edits or []:
        try:
            seg_id = int(edit.get("id"))
        except (TypeError, ValueError):
            continue
        seg = by_id.get(seg_id)
        if seg is None:
            continue

        if "speaker" in edit:
            new_speaker = (edit.get("speaker") or "").strip()
            if new_speaker != seg.get("speaker", ""):
                seg["speaker"] = new_speaker
                touched += 1

        if "text" in edit:
            new_text = (edit.get("text") or "").strip()
            if new_text != segment_text(seg):
                seg["text"] = new_text
                seg["edited"] = True
                if new_text:
                    seg["stale_timings"] = True
                else:
                    # Deliberately cleared. Drop the words too, otherwise the
                    # text fallback would resurrect the model's original.
                    seg["words"] = []
                    seg.pop("stale_timings", None)
                touched += 1

    if touched:
        doc["human_edited"] = True
        doc["updated"] = _now()
    return doc


def split_segment(doc: dict, seg_id: int, word_index: int) -> dict:
    """Split one segment before the given word index."""
    segments = doc.get("segments", [])
    for position, seg in enumerate(segments):
        if int(seg.get("id")) != int(seg_id):
            continue
        words = seg.get("words") or []
        if not 0 < word_index < len(words):
            return doc
        head, tail = words[:word_index], words[word_index:]
        first = make_segment(
            seg["id"], head[0]["start"], head[-1]["end"],
            " ".join(w["w"] for w in head), head,
            speaker=seg.get("speaker", ""), edited=True,
            avg_logprob=seg.get("avg_logprob"),
            no_speech_prob=seg.get("no_speech_prob"),
        )
        second = make_segment(
            seg["id"] + 1, tail[0]["start"], tail[-1]["end"],
            " ".join(w["w"] for w in tail), tail,
            speaker=seg.get("speaker", ""), edited=True,
            avg_logprob=seg.get("avg_logprob"),
            no_speech_prob=seg.get("no_speech_prob"),
        )
        segments[position:position + 1] = [first, second]
        doc["human_edited"] = True
        return normalise(doc)
    return doc


def merge_segment(doc: dict, seg_id: int) -> dict:
    """Merge the given segment with the one after it."""
    segments = doc.get("segments", [])
    for position, seg in enumerate(segments):
        if int(seg.get("id")) != int(seg_id):
            continue
        if position + 1 >= len(segments):
            return doc
        nxt = segments[position + 1]
        words = (seg.get("words") or []) + (nxt.get("words") or [])
        text = (segment_text(seg) + " " + segment_text(nxt)).strip()
        merged = make_segment(
            seg["id"],
            seg.get("start", 0.0),
            nxt.get("end", 0.0),
            text,
            words,
            speaker=seg.get("speaker") or nxt.get("speaker", ""),
            edited=True,
            avg_logprob=seg.get("avg_logprob"),
            no_speech_prob=seg.get("no_speech_prob"),
        )
        segments[position:position + 2] = [merged]
        doc["human_edited"] = True
        return normalise(doc)
    return doc


def paragraphs(doc: dict, gap: float = 1.5) -> list:
    """Group segments into paragraphs on silence gaps and speaker changes.

    Used by the .txt and .md exporters so the prose reads as prose rather than
    as a list of caption fragments.
    """
    out: list = []
    current: list = []
    last_end = None
    last_speaker = None

    for seg in doc.get("segments", []):
        text = segment_text(seg)
        if not text:
            continue
        speaker = seg.get("speaker") or ""
        start = float(seg.get("start") or 0.0)

        boundary = False
        if last_end is not None and gap > 0 and (start - last_end) >= gap:
            boundary = True
        if last_speaker is not None and speaker != last_speaker:
            boundary = True

        if boundary and current:
            out.append(current)
            current = []

        current.append(seg)
        last_end = float(seg.get("end") or start)
        last_speaker = speaker

    if current:
        out.append(current)
    return out
