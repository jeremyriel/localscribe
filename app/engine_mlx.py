"""MLX backend: Whisper transcription on Apple Silicon GPUs via Metal.

MLX is Apple's own array framework, built around unified memory - CPU and
GPU code read and write the same buffer, with no copy between them. That
makes it the natural way to use the GPU on a Mac for this app, unlike
CTranslate2 (what the rest of the engine uses), which has no Apple GPU
backend at all and runs Apple Silicon on CPU only.

This module is only ever imported from inside app/engine.py's functions,
never at module load time, and every ``mlx``/``mlx_whisper`` import in here
is itself deferred into function bodies. That guarantees importing
app.engine (and therefore starting the app) costs nothing on Windows,
Linux, or Intel Macs, where these packages have no wheel at all.

mlx-whisper's public ``transcribe()`` is a single blocking call over an
entire audio file, with no per-chunk callback. Calling it once per file
would break this app's live ETA and cooperative cancellation (see
WhisperEngine.transcribe in engine.py), so this module chunks the decoded
audio into fixed windows itself and calls mlx_whisper.transcribe() once per
window, translating each window's results back into the whole-file
timeline. The cost is coarser cancellation (bounded by one chunk instead of
one segment) and a small quality risk at chunk boundaries; both are
accepted trade-offs, documented in savestate.md, not oversights.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field

# Audio window fed to mlx_whisper per call. Long enough to keep MLX's own
# internal batching efficient, short enough that cancellation and the ETA
# stay responsive. Empirically chosen as a starting point, not tuned yet -
# see savestate.md verification notes before changing it.
CHUNK_SECONDS = 120
SAMPLE_RATE = 16_000


def available() -> bool:
    """Cheap check for whether mlx-whisper is installed, without importing it."""
    return (
        importlib.util.find_spec("mlx") is not None
        and importlib.util.find_spec("mlx_whisper") is not None
    )


# ---------------------------------------------------------------------------
# Shims: objects shaped exactly like the faster-whisper Segment/Word/
# TranscriptionInfo attributes that app/transcript.py's from_engine_segment
# and app/jobs.py's on_segment callback read. Keeping engine.py's shared
# transcription loop untouched (see transcribe() below) depends on these
# matching by attribute name exactly.
# ---------------------------------------------------------------------------


@dataclass
class EngineWord:
    word: str
    start: float
    end: float
    probability: float = 1.0


@dataclass
class EngineSegment:
    start: float
    end: float
    text: str
    words: list = field(default_factory=list)
    tokens: list = field(default_factory=list)
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None
    temperature: float | None = None


@dataclass
class EngineInfo:
    language: str = ""
    language_probability: float = 0.0
    duration_after_vad: float | None = None


class MlxLoadError(RuntimeError):
    """The MLX model could not be loaded."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_model(model_repo: str) -> None:
    """Force mlx-whisper to load and cache the model now.

    This makes model loading a distinct, timed console stage instead of
    hiding it inside the first transcribed chunk, matching the CTranslate2
    path's existing UX. Prefers mlx_whisper's internal loader (fast, no
    audio needed); if that function ever moves in a future mlx_whisper
    release, falls back to transcribing a fraction of a second of silence
    through the public transcribe() API, which forces the same load+cache
    path without depending on any private function.
    """
    try:
        from mlx_whisper.load_models import load_model as _load
        _load(model_repo)
        return
    except Exception:
        pass

    try:
        import numpy as np
        import mlx_whisper
        silence = np.zeros(SAMPLE_RATE // 4, dtype=np.float32)
        mlx_whisper.transcribe(silence, path_or_hf_repo=model_repo, word_timestamps=False)
    except Exception as exc:
        raise MlxLoadError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


def _translate_params(params: dict) -> dict:
    """Map the subset of settings mlx_whisper actually understands.

    mlx_whisper's transcribe() follows OpenAI Whisper's original signature,
    not faster-whisper/CTranslate2's - several knobs used elsewhere in this
    app (beam_size, best_of, patience, no_repeat_ngram_size, vad_filter,
    multilingual, chunk_length, the advanced-panel passthrough, ...) have no
    equivalent here. They are silently dropped rather than guessed at,
    since passing an unsupported keyword would raise instead of degrading
    gracefully. Revisit this mapping against the installed mlx_whisper
    version's actual signature before relying on it for anything beyond the
    basics below.
    """
    out: dict = {"word_timestamps": True}
    passthrough = (
        "language", "task", "temperature", "condition_on_previous_text",
        "initial_prompt", "compression_ratio_threshold", "no_speech_threshold",
        "hallucination_silence_threshold",
    )
    for key in passthrough:
        if key in params:
            out[key] = params[key]
    # faster-whisper/CTranslate2 spells this log_prob_threshold; the
    # original Whisper API (and mlx_whisper) spells it logprob_threshold.
    if "log_prob_threshold" in params:
        out["logprob_threshold"] = params["log_prob_threshold"]
    return out


def _segments_from_result(result: dict, chunk_start: float):
    """Yield EngineSegments from one mlx_whisper.transcribe() call's output,
    with timestamps shifted from chunk-relative to whole-file time."""
    for raw in result.get("segments") or []:
        words = [
            EngineWord(
                word=w.get("word", ""),
                start=chunk_start + float(w.get("start", 0.0)),
                end=chunk_start + float(w.get("end", 0.0)),
                probability=float(w.get("probability", 1.0)),
            )
            for w in (raw.get("words") or [])
        ]
        yield EngineSegment(
            start=chunk_start + float(raw.get("start", 0.0)),
            end=chunk_start + float(raw.get("end", 0.0)),
            text=raw.get("text", ""),
            words=words,
            tokens=raw.get("tokens") or [],
            avg_logprob=raw.get("avg_logprob"),
            no_speech_prob=raw.get("no_speech_prob"),
            compression_ratio=raw.get("compression_ratio"),
            temperature=None,
        )


def transcribe(pcm, *, model_repo: str, params: dict, should_cancel=None):
    """Transcribe decoded PCM via mlx_whisper, chunked into fixed windows.

    Mirrors faster-whisper's ``model.transcribe()`` contract: returns
    ``(segments_iterator, info)`` where segments decode lazily as the
    iterator is consumed. This lets WhisperEngine.transcribe()'s existing
    loop (tracker updates, on_segment callbacks, cancellation) drive this
    backend identically to the CTranslate2 one, with no changes needed
    there beyond choosing which callable produces the iterator.

    ``pcm`` is float32 mono 16 kHz, matching what app/media.py produces.
    """
    import mlx_whisper

    mlx_params = _translate_params(params)
    mlx_params["path_or_hf_repo"] = model_repo

    chunk_samples = int(CHUNK_SECONDS * SAMPLE_RATE)
    total_samples = len(pcm)

    # Decode the first chunk eagerly so `info` (detected language) is ready
    # immediately, matching faster-whisper's contract where `info` is
    # available before the caller starts iterating segments.
    first_result = mlx_whisper.transcribe(pcm[:chunk_samples], **mlx_params)
    language = first_result.get("language") or ""
    if "language" not in mlx_params and language:
        # Keep later chunks consistent with what the first chunk detected,
        # rather than re-detecting (and potentially disagreeing) per chunk.
        mlx_params["language"] = language

    info = EngineInfo(
        language=language,
        language_probability=1.0 if language else 0.0,
        duration_after_vad=None,
    )

    def _segments():
        yield from _segments_from_result(first_result, chunk_start=0.0)
        offset = chunk_samples
        while offset < total_samples:
            if should_cancel is not None and should_cancel():
                from .engine import Cancelled
                raise Cancelled()
            result = mlx_whisper.transcribe(pcm[offset:offset + chunk_samples], **mlx_params)
            yield from _segments_from_result(result, chunk_start=offset / SAMPLE_RATE)
            offset += chunk_samples

    return _segments(), info
