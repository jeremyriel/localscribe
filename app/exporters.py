"""Transcript exporters: .txt, .md, .docx, .vtt, .srt, .json.

Every writer is a pure projection of ``transcript.json``, so the caption files
and the prose files can never disagree about what was said or when. Re-running
the exporters after a re-timestamp is all it takes to bring every output back
into agreement.

Caption files are built from *word* timings rather than segment timings, which
is what lets a cue break at a sensible place instead of wherever the model
happened to end a segment.
"""

from __future__ import annotations

import json
from pathlib import Path

from .transcript import (
    LOW_CONFIDENCE,
    full_text,
    paragraphs,
    segment_text,
    speakers,
    summary,
    word_count,
)

ALL_FORMATS = ("txt", "md", "docx", "vtt", "srt", "json")


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------

def vtt_time(seconds: float) -> str:
    """WebVTT uses a dot before milliseconds."""
    return _clock(seconds, ".")


def srt_time(seconds: float) -> str:
    """SubRip uses a comma before milliseconds."""
    return _clock(seconds, ",")


def _clock(seconds: float, sep: str) -> str:
    total_ms = int(round(max(0.0, float(seconds)) * 1000.0))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{ms:03d}"


def stamp(seconds: float) -> str:
    """hh:mm:ss used in readable transcripts."""
    total = int(round(max(0.0, float(seconds))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# ---------------------------------------------------------------------------
# Caption cue building
# ---------------------------------------------------------------------------

def build_cues(
    doc: dict,
    max_chars: int = 42,
    max_lines: int = 2,
    max_duration: float = 6.0,
) -> list:
    """Split the transcript into caption cues at word boundaries.

    A cue is closed when adding the next word would exceed the character
    budget (``max_chars`` x ``max_lines``), when the cue would run longer than
    ``max_duration``, or at a segment boundary. Words are never split, so the
    result is always readable.
    """
    max_chars = max(10, int(max_chars))
    max_lines = max(1, int(max_lines))
    cues: list = []

    for seg in doc.get("segments", []):
        words = seg.get("words") or []
        speaker = (seg.get("speaker") or "").strip()

        if not words:
            # No word timings (an edited segment awaiting a re-timestamp, or a
            # hand-typed segment): emit the whole segment as one cue rather
            # than dropping it from the captions.
            text = segment_text(seg)
            if text:
                cues.append({
                    "start": float(seg.get("start") or 0.0),
                    "end": float(seg.get("end") or 0.0),
                    "speaker": speaker,
                    "text": text,
                })
            continue

        # A speaker tag is rendered inside the cue, so it must count against
        # the line budget or tagged cues overflow. VTT writes "<v Name>" and
        # SRT writes "Name: "; reserve the longer of the two so one cue list
        # can serve both writers.
        reserve = max(len(f"<v {speaker}>"), len(f"{speaker}: ")) if speaker else 0

        current: list = []
        for word in words:
            token = word.get("w", "")
            if not token:
                continue
            prospective = " ".join([w["w"] for w in current] + [token])
            duration = float(word["end"]) - float(current[0]["start"]) if current else 0.0

            too_long = not fits(prospective, max_chars, max_lines, reserve)
            too_slow = duration > float(max_duration)
            if current and (too_long or too_slow):
                cues.append(_cue(current, speaker))
                current = []
            current.append(word)

        if current:
            cues.append(_cue(current, speaker))

    return cues


def _cue(words: list, speaker: str) -> dict:
    return {
        "start": float(words[0]["start"]),
        "end": float(words[-1]["end"]),
        "speaker": speaker,
        "text": " ".join(w["w"] for w in words).strip(),
    }


def wrap_lines(text: str, max_chars: int, max_lines: int | None = None) -> list:
    """Greedy word wrap at ``max_chars``, breaking only between words.

    ``max_lines`` is accepted for call-site clarity but never used to force an
    overlong line: a single word longer than the limit gets its own line rather
    than being split, because splitting a word mid-character is worse than a
    slightly wide caption. ``build_cues`` is responsible for closing a cue
    before it needs more lines than allowed.
    """
    lines: list = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def fits(text: str, max_chars: int, max_lines: int, reserve: int = 0) -> bool:
    """True when text wraps into max_lines lines of at most max_chars.

    ``reserve`` accounts for a prefix rendered into the first line, such as a
    speaker tag. Checking the real wrap rather than a characters-times-lines
    budget matters because greedy wrapping is not optimal: 84 characters do not
    always fit into two 42-character lines.
    """
    # The prefix is padded with a non-space filler because wrap_lines splits
    # on whitespace and would otherwise discard leading spaces entirely.
    probe = ("x" * reserve) + text if reserve else text
    return len(wrap_lines(probe, max_chars)) <= max_lines


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def write_vtt(doc: dict, dest: Path, settings: dict, meta: dict | None = None) -> Path:
    max_chars = int(settings.get("vtt_max_chars", 42))
    max_lines = int(settings.get("vtt_max_lines", 2))
    max_duration = float(settings.get("vtt_max_duration", 6.0))

    cues = build_cues(doc, max_chars, max_lines, max_duration)
    out = ["WEBVTT", ""]

    title = (meta or {}).get("title") or ""
    if title:
        # NOTE comments are valid WebVTT and ignored by players.
        out += [f"NOTE {title}", ""]

    for index, cue in enumerate(cues, start=1):
        body = cue["text"]
        if cue["speaker"]:
            body = f"<v {cue['speaker']}>{body}"
        lines = wrap_lines(body, max_chars, max_lines)
        out.append(str(index))
        out.append(f"{vtt_time(cue['start'])} --> {vtt_time(cue['end'])}")
        out.extend(lines)
        out.append("")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out), encoding="utf-8")
    return dest


def write_srt(doc: dict, dest: Path, settings: dict, meta: dict | None = None) -> Path:
    max_chars = int(settings.get("vtt_max_chars", 42))
    max_lines = int(settings.get("vtt_max_lines", 2))
    max_duration = float(settings.get("vtt_max_duration", 6.0))

    cues = build_cues(doc, max_chars, max_lines, max_duration)
    out = []
    for index, cue in enumerate(cues, start=1):
        body = cue["text"]
        if cue["speaker"]:
            body = f"{cue['speaker']}: {body}"
        out.append(str(index))
        out.append(f"{srt_time(cue['start'])} --> {srt_time(cue['end'])}")
        out.extend(wrap_lines(body, max_chars, max_lines))
        out.append("")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out), encoding="utf-8")
    return dest


def write_txt(doc: dict, dest: Path, settings: dict, meta: dict | None = None) -> Path:
    include_ts = bool(settings.get("txt_include_timestamps", False))
    include_speakers = bool(settings.get("txt_include_speakers", True))
    gap = float(settings.get("paragraph_gap", 1.5))
    meta = meta or {}

    lines: list = []
    title = meta.get("title") or meta.get("filename") or "Transcript"
    lines.append(title)
    lines.append("=" * len(title))
    lines.append("")

    header = _plain_header(doc, meta)
    if header:
        lines.extend(header)
        lines.append("")

    for group in paragraphs(doc, gap=gap):
        prefix_parts = []
        if include_ts:
            prefix_parts.append(f"[{stamp(group[0].get('start') or 0.0)}]")
        speaker = (group[0].get("speaker") or "").strip()
        if include_speakers and speaker:
            prefix_parts.append(f"{speaker}:")
        prefix = " ".join(prefix_parts)
        body = " ".join(segment_text(s) for s in group if segment_text(s)).strip()
        lines.append(f"{prefix} {body}".strip() if prefix else body)
        lines.append("")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return dest


def _plain_header(doc: dict, meta: dict) -> list:
    info = summary(doc)
    rows = []
    if meta.get("project"):
        rows.append(f"Project: {meta['project']}")
    if meta.get("filename"):
        rows.append(f"Source file: {meta['filename']}")
    rows.append(f"Duration: {stamp(info['duration'])}")
    rows.append(f"Words: {info['words']}   Segments: {info['segments']}")
    if info["language"]:
        rows.append(f"Language: {info['language']}")
    rows.append(f"Model: {info['model']} (OpenAI Whisper, run locally)")
    if info["speakers"]:
        rows.append(f"Speakers: {', '.join(info['speakers'])}")
    rows.append(
        "Review status: "
        + (
            "human-reviewed" if info["human_edited"] else
            "machine output, not yet reviewed"
        )
    )
    if meta.get("generated"):
        rows.append(f"Exported: {meta['generated']}")
    return rows


def write_md(doc: dict, dest: Path, settings: dict, meta: dict | None = None) -> Path:
    gap = float(settings.get("paragraph_gap", 1.5))
    include_ts = bool(settings.get("txt_include_timestamps", False))
    include_speakers = bool(settings.get("txt_include_speakers", True))
    meta = meta or {}
    info = summary(doc)

    def esc(value) -> str:
        text = str(value if value is not None else "")
        return text.replace('"', '\\"')

    lines = ["---"]
    lines.append(f'title: "{esc(meta.get("title") or meta.get("filename") or "Transcript")}"')
    if meta.get("project"):
        lines.append(f'project: "{esc(meta["project"])}"')
    if meta.get("filename"):
        lines.append(f'source_file: "{esc(meta["filename"])}"')
    lines.append(f"duration_seconds: {info['duration']}")
    lines.append(f"duration: \"{stamp(info['duration'])}\"")
    lines.append(f"words: {info['words']}")
    lines.append(f"segments: {info['segments']}")
    if info["language"]:
        lines.append(f'language: "{esc(info["language"])}"')
    lines.append(f'model: "{esc(info["model"])}"')
    lines.append("engine: faster-whisper (CTranslate2), executed locally")
    lines.append(f"human_reviewed: {str(bool(info['human_edited'])).lower()}")
    if info["retimestamped_at"]:
        lines.append(f'retimestamped_at: "{esc(info["retimestamped_at"])}"')
    if info["speakers"]:
        lines.append("speakers:")
        for name in info["speakers"]:
            lines.append(f'  - "{esc(name)}"')
    for key in ("principal_investigator", "irb_protocol", "participant_id"):
        if meta.get(key):
            lines.append(f'{key}: "{esc(meta[key])}"')
    if meta.get("generated"):
        lines.append(f'exported: "{esc(meta["generated"])}"')
    lines.append('processing: "Transcribed offline. No audio or text left this machine."')
    lines.append("---")
    lines.append("")

    lines.append(f"# {meta.get('title') or meta.get('filename') or 'Transcript'}")
    lines.append("")
    if meta.get("description"):
        lines.append(f"_{meta['description']}_")
        lines.append("")

    lines.append("## Transcript")
    lines.append("")

    for group in paragraphs(doc, gap=gap):
        speaker = (group[0].get("speaker") or "").strip()
        parts = []
        if include_speakers and speaker:
            parts.append(f"**{speaker}**")
        if include_ts:
            parts.append(f"`{stamp(group[0].get('start') or 0.0)}`")
        body = " ".join(segment_text(s) for s in group if segment_text(s)).strip()
        prefix = " ".join(parts)
        lines.append(f"{prefix} {body}".strip() if prefix else body)
        lines.append("")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return dest


def write_json(doc: dict, dest: Path, settings: dict, meta: dict | None = None) -> Path:
    payload = {
        "meta": meta or {},
        "summary": summary(doc),
        "transcript": doc,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return dest


def write_docx(doc: dict, dest: Path, settings: dict, meta: dict | None = None) -> Path:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    gap = float(settings.get("paragraph_gap", 1.5))
    include_ts = bool(settings.get("txt_include_timestamps", False))
    include_speakers = bool(settings.get("txt_include_speakers", True))
    meta = meta or {}
    info = summary(doc)

    document = Document()

    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    title = meta.get("title") or meta.get("filename") or "Transcript"
    heading = document.add_heading(title, level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.LEFT

    subtitle = document.add_paragraph()
    run = subtitle.add_run("Transcribed locally with OpenAI Whisper - no data left this machine")
    run.italic = True
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    # Metadata table: the provenance a research record needs.
    rows = [
        ("Source file", meta.get("filename", "")),
        ("Project", meta.get("project", "")),
        ("Duration", stamp(info["duration"])),
        ("Words", str(info["words"])),
        ("Segments", str(info["segments"])),
        ("Language", info["language"] or "not recorded"),
        ("Model", info["model"]),
        ("Review status", "Human-reviewed" if info["human_edited"]
         else "Machine output, not yet reviewed"),
    ]
    for key in ("principal_investigator", "irb_protocol", "participant_id"):
        if meta.get(key):
            rows.append((key.replace("_", " ").title(), str(meta[key])))
    if info["speakers"]:
        rows.append(("Speakers", ", ".join(info["speakers"])))
    if meta.get("generated"):
        rows.append(("Exported", str(meta["generated"])))

    table = document.add_table(rows=0, cols=2)
    table.style = "Light Grid Accent 1"
    for label, value in rows:
        if not value:
            continue
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = str(value)
        for paragraph in cells[0].paragraphs:
            for r in paragraph.runs:
                r.bold = True
                r.font.size = Pt(9)
        for paragraph in cells[1].paragraphs:
            for r in paragraph.runs:
                r.font.size = Pt(9)

    document.add_paragraph()
    document.add_heading("Transcript", level=1)

    for group in paragraphs(doc, gap=gap):
        speaker = (group[0].get("speaker") or "").strip()
        body = " ".join(segment_text(s) for s in group if segment_text(s)).strip()
        if not body:
            continue
        paragraph = document.add_paragraph()
        if include_ts:
            ts_run = paragraph.add_run(f"[{stamp(group[0].get('start') or 0.0)}] ")
            ts_run.font.size = Pt(9)
            ts_run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
        if include_speakers and speaker:
            sp_run = paragraph.add_run(f"{speaker}: ")
            sp_run.bold = True
        paragraph.add_run(body)

    if meta.get("notes"):
        document.add_heading("Project notes", level=1)
        document.add_paragraph(str(meta["notes"]))

    dest.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(dest))
    return dest


WRITERS = {
    "txt": write_txt,
    "md": write_md,
    "docx": write_docx,
    "vtt": write_vtt,
    "srt": write_srt,
    "json": write_json,
}


def export_all(
    doc: dict,
    out_dir: Path,
    settings: dict,
    meta: dict | None = None,
    formats=None,
    basename: str = "transcript",
) -> dict:
    """Write every requested format. Returns {format: path or error string}."""
    wanted = list(formats or settings.get("formats") or ALL_FORMATS)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: dict = {}
    for fmt in wanted:
        writer = WRITERS.get(fmt)
        if writer is None:
            continue
        dest = out_dir / f"{basename}.{fmt}"
        try:
            written[fmt] = str(writer(doc, dest, settings, meta))
        except Exception as exc:
            written[fmt] = f"error: {exc}"
    return written


def preview_stats(doc: dict) -> dict:
    """Small summary used by the UI after an export."""
    info = summary(doc)
    return {
        "words": info["words"],
        "segments": info["segments"],
        "characters": len(full_text(doc)),
        "low_confidence": info["low_confidence"],
        "low_confidence_threshold": LOW_CONFIDENCE,
        "speakers": speakers(doc),
        "word_total": word_count(doc),
    }
