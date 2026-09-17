"""The canonical transcript document.

``transcript.json`` is the single source of truth for a document. Every export
(.txt, .md, .docx, .vtt, .srt) is a pure projection of it, and the editor reads
and writes it directly. Keeping one authoritative structure is what makes the
re-timestamp pass and the caption files agree with each other.

Shape
-----
::

    {
      "schema": 3,
      "created": "...", "updated": "...",
      "language": "en", "language_probability": 0.99,
      "duration": 3612.4,
      "model": "large-v3-turbo",
      "engine": {...},          # settings actually used, for reproducibility
      "stats": {...},           # throughput measured during transcription
      "human_edited": false,
      "speakers": [             # the roster; see "Speakers" below
        {"id": "s1", "name": "Interviewer", "short": "INT", "color": "#0f766e"}
      ],
      "dictionary": ["TRAILblazer"],   # words the validator accepted
      "segments": [
        {"id": 0, "start": 0.0, "end": 4.2, "text": "...",
         "speaker_id": "s1", "speaker": "Interviewer", "edited": false,
         "words": [{"w": "Hello", "start": 0.1, "end": 0.4, "prob": 0.98}],
         "avg_logprob": -0.21, "no_speech_prob": 0.01,
         "compression_ratio": 1.4, "temperature": 0.0}
      ]
    }

Speakers and turns
------------------
``speaker_id`` is authoritative and ``speaker`` is the display name resolved
from the roster on every normalise, so the two cannot drift. Keeping the
resolved name means the exporters, the VTT voice tag and ``summary()`` need no
knowledge of the roster, and a transcript opened in a text editor still reads.

A **turn** is a maximal run of consecutive segments sharing one speaker. That
definition is what makes the display rule fall out for free: consecutive turns
always differ in speaker, so printing the name once per turn means it is never
repeated while the same person is still talking. Pauses inside a turn are
recorded rather than used as boundaries, so a silence breaks the paragraph
without reprinting the name.

Schema 2 documents, which stored a free-text speaker string per segment,
migrate automatically on load: distinct names become roster entries and the
existing tagging is preserved.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

SCHEMA_VERSION = 3

# Confidence below which the editor tints a word for attention.
LOW_CONFIDENCE = 0.55

# Pause inside one speaker's turn that is long enough to show as a break.
DEFAULT_PAUSE_GAP = 1.5

_WORD_SPLIT = re.compile(r"\S+")

# ---------------------------------------------------------------------------
# Speaker palette
# ---------------------------------------------------------------------------
# The Okabe-Ito qualitative palette, which stays distinguishable under all
# common forms of colour vision deficiency. Two entries are darkened from the
# published values so they hold contrast against the warm paper background.
#
# Colour is never the only channel that identifies a speaker: the acronym is
# always rendered as text beside it, so the interface remains usable in
# greyscale and for anyone who cannot distinguish these hues (WCAG 1.4.1).
PALETTE = (
    "#0f766e",  # teal - matches the application accent
    "#b45309",  # amber
    "#1d5fa8",  # blue
    "#9a3f8f",  # purple
    "#166534",  # green
    "#a3341f",  # vermillion
    "#7a5c00",  # olive
    "#3f6d86",  # slate blue
)


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
        "speakers": [],
        "dictionary": [],
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
        # `speaker_id` is authoritative; `speaker` is the resolved display name,
        # rewritten from the roster on every normalise so the two cannot drift.
        "speaker_id": meta.get("speaker_id", ""),
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
# Speakers
# ---------------------------------------------------------------------------

def derive_short(name: str, taken=()) -> str:
    """Derive an acronym from a speaker name, avoiding collisions.

    "Participant 04" -> "P04", "Interviewer" -> "INT", "Ana Maria Ruiz" -> "AMR".
    The validator can always override it; this only supplies a sensible start.
    """
    text = (name or "").strip()
    if not text:
        return ""
    taken = {t.upper() for t in taken if t}

    parts = [p for p in re.split(r"[\s_-]+", text) if p]
    if len(parts) >= 2:
        # Keep a trailing number whole: "Participant 04" reads better as P04.
        if re.fullmatch(r"\d+", parts[-1]):
            candidate = parts[0][0] + parts[-1]
        else:
            candidate = "".join(p[0] for p in parts[:3])
    else:
        # A single word: strip vowels after the first letter for a compact tag.
        word = parts[0]
        letters = word[0] + re.sub(r"[aeiou]", "", word[1:], flags=re.I)
        candidate = (letters or word)[:3]

    candidate = candidate.upper()[:4]
    if candidate not in taken:
        return candidate

    for suffix in range(2, 100):
        probe = f"{candidate[:3]}{suffix}"
        if probe not in taken:
            return probe
    return candidate


def new_speaker(index: int, name: str = "", roster=()) -> dict:
    """Build a roster entry. `index` drives the default name and colour."""
    label = (name or "").strip() or f"Speaker {index + 1}"
    taken_ids = {s.get("id") for s in roster}
    speaker_id = f"s{index + 1}"
    bump = index + 1
    while speaker_id in taken_ids:
        bump += 1
        speaker_id = f"s{bump}"
    return {
        "id": speaker_id,
        "name": label,
        "short": derive_short(label, [s.get("short") for s in roster]),
        "color": PALETTE[index % len(PALETTE)],
    }


def speaker_roster(doc: dict) -> list:
    return doc.get("speakers") or []


def speaker_by_id(doc: dict, speaker_id: str) -> dict | None:
    if not speaker_id:
        return None
    for speaker in speaker_roster(doc):
        if speaker.get("id") == speaker_id:
            return speaker
    return None


def clean_roster(roster) -> list:
    """Validate an incoming roster, filling gaps and de-duplicating ids."""
    cleaned: list = []
    seen_ids: set = set()
    for index, raw in enumerate(roster or []):
        if not isinstance(raw, dict):
            continue
        speaker_id = str(raw.get("id") or "").strip() or f"s{index + 1}"
        while speaker_id in seen_ids:
            speaker_id = f"{speaker_id}x"
        seen_ids.add(speaker_id)

        name = str(raw.get("name") or "").strip() or f"Speaker {index + 1}"
        short = str(raw.get("short") or "").strip()[:6]
        if not short:
            short = derive_short(name, [s["short"] for s in cleaned])
        color = str(raw.get("color") or "").strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            color = PALETTE[index % len(PALETTE)]

        cleaned.append({
            "id": speaker_id, "name": name, "short": short, "color": color,
        })
    return cleaned


def set_roster(doc: dict, roster) -> dict:
    """Replace the roster, unassigning segments whose speaker disappeared.

    Returns a report naming how many segments were unassigned, so the caller
    can tell the user rather than silently orphaning their tagging work.
    """
    cleaned = clean_roster(roster)
    valid = {s["id"] for s in cleaned}

    orphaned = 0
    lost: dict = {}
    for seg in doc.get("segments", []):
        current = seg.get("speaker_id") or ""
        if current and current not in valid:
            previous = speaker_by_id(doc, current)
            lost[current] = (previous or {}).get("name", current)
            seg["speaker_id"] = ""
            orphaned += 1

    doc["speakers"] = cleaned
    resolve_speakers(doc)
    doc["updated"] = _now()
    return {
        "unassigned_segments": orphaned,
        "removed_speakers": sorted(lost.values()),
        "speakers": cleaned,
    }


def resolve_speakers(doc: dict) -> dict:
    """Rewrite every segment's display name from its speaker id.

    `speaker` exists so that the exporters, the VTT voice tag and
    `summary()` keep working unchanged, and so an old transcript opened in a
    text editor still reads sensibly. It is derived, never authoritative.
    """
    lookup = {s.get("id"): s for s in speaker_roster(doc)}
    for seg in doc.get("segments", []):
        speaker_id = seg.get("speaker_id") or ""
        if speaker_id and speaker_id in lookup:
            seg["speaker"] = lookup[speaker_id].get("name", "")
        elif not speaker_id:
            seg["speaker"] = ""
    return doc


def assign_speaker(doc: dict, segment_ids, speaker_id: str) -> int:
    """Tag the given segments with a speaker. Empty id clears the tag."""
    wanted = {int(s) for s in segment_ids or []}
    if speaker_id and speaker_by_id(doc, speaker_id) is None:
        raise ValueError(f"No speaker with id {speaker_id!r} in this transcript.")

    changed = 0
    for seg in doc.get("segments", []):
        if int(seg.get("id", -1)) not in wanted:
            continue
        if (seg.get("speaker_id") or "") != (speaker_id or ""):
            seg["speaker_id"] = speaker_id or ""
            changed += 1

    if changed:
        resolve_speakers(doc)
        doc["human_edited"] = True
        doc["updated"] = _now()
    return changed


def migrate_speakers(doc: dict) -> dict:
    """Build a roster from legacy free-text speaker strings.

    Schema 2 stored the speaker as a bare string per segment. Any distinct
    non-empty value becomes a roster entry, keeping the tagging work already
    done. Idempotent: once every segment carries a `speaker_id`, this is a
    no-op.
    """
    if doc.get("speakers"):
        # A roster exists; just make sure legacy strings still map onto it.
        by_name = {s.get("name"): s.get("id") for s in doc["speakers"]}
        for seg in doc.get("segments", []):
            if not seg.get("speaker_id") and seg.get("speaker") in by_name:
                seg["speaker_id"] = by_name[seg["speaker"]]
        return doc

    names: list = []
    for seg in doc.get("segments", []):
        name = (seg.get("speaker") or "").strip()
        if name and name not in names:
            names.append(name)

    roster: list = []
    for index, name in enumerate(names):
        roster.append(new_speaker(index, name, roster))

    doc["speakers"] = roster
    by_name = {s["name"]: s["id"] for s in roster}
    for seg in doc.get("segments", []):
        name = (seg.get("speaker") or "").strip()
        seg["speaker_id"] = by_name.get(name, "")
    return doc


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------

def turns(doc: dict, gap: float = DEFAULT_PAUSE_GAP) -> list:
    """Group segments into speaking turns.

    A turn is a *maximal run of consecutive segments sharing one speaker*.
    Defining it that way makes the display rule fall out for free: consecutive
    turns always differ in speaker, so rendering the name once per turn means
    it is never repeated while the same person is still talking -- which is
    exactly what a transcript should do.

    Pauses at or above `gap` *within* a turn are recorded rather than used as
    boundaries, so a long silence shows as a break in the text without
    reprinting the speaker's name.

    Untagged segments (no speaker_id) are grouped at pauses instead, so an
    untranscribed-by-speaker document still arrives as sensible, clickable
    units for the validator to tag.
    """
    grouped: list = []
    current: dict | None = None
    previous_end: float | None = None

    for seg in doc.get("segments", []):
        speaker_id = seg.get("speaker_id") or ""
        start = float(seg.get("start") or 0.0)
        pause = (start - previous_end) if previous_end is not None else 0.0

        if current is None:
            boundary = True
        elif speaker_id != current["speaker_id"]:
            boundary = True
        elif not speaker_id and gap > 0 and pause >= gap:
            # Untagged: split on pauses so there is something to click.
            boundary = True
        else:
            boundary = False

        if boundary:
            current = {
                "speaker_id": speaker_id,
                "segments": [],
                "pauses": [],
                "start": start,
                "end": float(seg.get("end") or start),
            }
            grouped.append(current)
        elif gap > 0 and pause >= gap:
            # A pause inside a turn: recorded against the segment that follows
            # it, so the renderer can break there without a new speaker label.
            current["pauses"].append({
                "before": int(seg.get("id", 0)),
                "seconds": round(pause, 2),
            })

        current["segments"].append(seg)
        current["end"] = float(seg.get("end") or start)
        previous_end = current["end"]

    for index, turn in enumerate(grouped):
        turn["index"] = index
        turn["ids"] = [int(s.get("id", 0)) for s in turn["segments"]]
    return grouped


def turn_text(turn: dict) -> str:
    return " ".join(
        t for t in (segment_text(s) for s in turn.get("segments", [])) if t
    ).strip()


def turn_blocks(turn: dict) -> list:
    """Split one turn into paragraph blocks at its recorded pauses.

    Returns [{"segments": [...], "pause_before": float|None}], so a writer can
    render a break and an optional marker without reprinting the speaker.
    """
    pauses = {p["before"]: p["seconds"] for p in turn.get("pauses", [])}
    blocks: list = []
    current: dict | None = None

    for seg in turn.get("segments", []):
        seg_id = int(seg.get("id", 0))
        if current is None or seg_id in pauses:
            current = {
                "segments": [],
                "pause_before": pauses.get(seg_id) if current is not None else None,
            }
            blocks.append(current)
        current["segments"].append(seg)
    return blocks


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

    doc.setdefault("speakers", [])
    doc.setdefault("dictionary", [])
    migrate_speakers(doc)

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
    doc["schema"] = SCHEMA_VERSION
    resolve_speakers(doc)
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

        if "speaker_id" in edit:
            wanted = str(edit.get("speaker_id") or "").strip()
            if wanted and speaker_by_id(doc, wanted) is None:
                wanted = ""
            if wanted != (seg.get("speaker_id") or ""):
                seg["speaker_id"] = wanted
                touched += 1

        elif "speaker" in edit:
            # A bare name rather than an id. Resolve it against the roster,
            # adding an entry when it is new, so a transcript edited by some
            # other tool -- or an older client -- still tags correctly instead
            # of having the name silently dropped by resolve_speakers().
            name = (edit.get("speaker") or "").strip()
            if not name:
                if seg.get("speaker_id"):
                    seg["speaker_id"] = ""
                    touched += 1
            else:
                match = next(
                    (s for s in speaker_roster(doc) if s.get("name") == name), None
                )
                if match is None:
                    roster = speaker_roster(doc)
                    match = new_speaker(len(roster), name, roster)
                    doc.setdefault("speakers", []).append(match)
                if seg.get("speaker_id") != match["id"]:
                    seg["speaker_id"] = match["id"]
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
        resolve_speakers(doc)
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


def paragraphs(doc: dict, gap: float = DEFAULT_PAUSE_GAP) -> list:
    """Flat list of paragraph blocks, for callers that do not need turns.

    Expressed in terms of ``turns()`` so that prose output and the editor
    agree about where a turn starts. Prefer ``turns()`` directly when the
    speaker label matters: this helper deliberately loses the distinction
    between "a new speaker" and "the same speaker after a pause", which is the
    distinction that decides whether to print a name.
    """
    out: list = []
    for turn in turns(doc, gap=gap):
        for block in turn_blocks(turn):
            segments = [s for s in block["segments"] if segment_text(s)]
            if segments:
                out.append(segments)
    return out
