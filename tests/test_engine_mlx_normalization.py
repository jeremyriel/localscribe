"""Tests for the MLX backend's normalization layer and device selection.

Pure logic only: no mlx/mlx_whisper import, no real model or audio. Proves
the MLX adapter's output is interoperable with app/transcript.py's schema
and that engine.py's device-selection branches make the right call for a
given hardware probe, without needing either faster-whisper or mlx-whisper
installed.

Run with:  .venv/bin/python -m tests.test_engine_mlx_normalization
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import catalog
from app import engine_mlx as M
from app.engine import EngineError, WhisperEngine
from app.transcript import from_engine_segment

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# engine_mlx: parameter translation
# ---------------------------------------------------------------------------

def test_translate_params_keeps_known_keys_drops_unknown():
    params = {
        "task": "transcribe",
        "language": "en",
        "temperature": (0.0, 0.2, 0.4),
        "condition_on_previous_text": True,
        "initial_prompt": "glossary terms",
        "compression_ratio_threshold": 2.4,
        "no_speech_threshold": 0.6,
        "log_prob_threshold": -1.0,
        "hallucination_silence_threshold": 2.0,
        # CTranslate2-only knobs with no MLX equivalent - must be dropped.
        "beam_size": 5,
        "best_of": 5,
        "patience": 1.0,
        "no_repeat_ngram_size": 0,
        "vad_filter": True,
        "vad_parameters": {"threshold": 0.5},
        "multilingual": True,
        "chunk_length": 30,
    }
    out = M._translate_params(params)

    check("keeps task", out.get("task") == "transcribe")
    check("keeps language", out.get("language") == "en")
    check("keeps temperature ladder", out.get("temperature") == (0.0, 0.2, 0.4))
    check("keeps initial_prompt", out.get("initial_prompt") == "glossary terms")
    check(
        "renames log_prob_threshold to logprob_threshold",
        out.get("logprob_threshold") == -1.0 and "log_prob_threshold" not in out,
    )
    check("always forces word_timestamps", out.get("word_timestamps") is True)
    for dropped in ("beam_size", "best_of", "patience", "no_repeat_ngram_size",
                    "vad_filter", "vad_parameters", "multilingual", "chunk_length"):
        check(f"drops unsupported '{dropped}'", dropped not in out)


# ---------------------------------------------------------------------------
# engine_mlx: chunk-offset math
# ---------------------------------------------------------------------------

def test_segments_from_result_offsets_timestamps():
    result = {
        "segments": [
            {
                "start": 1.0, "end": 3.5, "text": " hello there",
                "words": [
                    {"word": "hello", "start": 1.0, "end": 1.5, "probability": 0.95},
                    {"word": "there", "start": 1.6, "end": 2.0, "probability": 0.9},
                ],
                "avg_logprob": -0.2, "no_speech_prob": 0.01, "compression_ratio": 1.1,
            },
        ],
    }
    segments = list(M._segments_from_result(result, chunk_start=120.0))
    check("one segment produced", len(segments) == 1)
    seg = segments[0]
    check("segment start shifted", seg.start == 121.0, str(seg.start))
    check("segment end shifted", seg.end == 123.5, str(seg.end))
    check("segment text preserved", seg.text == " hello there")
    check("word count preserved", len(seg.words) == 2)
    check("word start shifted", seg.words[0].start == 121.0, str(seg.words[0].start))
    check("word end shifted", seg.words[1].end == 122.0, str(seg.words[1].end))
    check("word probability preserved", seg.words[0].probability == 0.95)


# ---------------------------------------------------------------------------
# engine_mlx: weight-filename alias (regression coverage for the real bug
# this was written to fix: mlx-community/whisper-large-v3-turbo-4bit ships
# its weights as "model.safetensors" instead of the "weights.safetensors"/
# "weights.npz" names mlx_whisper's loader hardcodes, which made loading it
# fail with a confusing "[load_npz] Input must be a zip file..." error -
# mlx_whisper silently fell through to a nonexistent weights.npz path.
# _ensure_weight_filename() must alias the one weight file present to a
# name the loader will actually find.
# ---------------------------------------------------------------------------

def test_ensure_weight_filename_aliases_a_single_oddly_named_file():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "config.json").write_text("{}")
        (Path(tmp) / "model.safetensors").write_bytes(b"fake weights")
        M._ensure_weight_filename(tmp)
        alias = Path(tmp) / "weights.safetensors"
        check("alias created", alias.exists())
        check(
            "alias points at the real weight file",
            alias.resolve().name == "model.safetensors",
        )


def test_ensure_weight_filename_is_a_noop_when_expected_name_present():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "weights.npz").write_bytes(b"fake weights")
        M._ensure_weight_filename(tmp)
        check(
            "no alias created when weights.npz already exists",
            not (Path(tmp) / "weights.safetensors").exists(),
        )


def test_ensure_weight_filename_is_a_noop_when_ambiguous():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "a.safetensors").write_bytes(b"one")
        (Path(tmp) / "b.safetensors").write_bytes(b"two")
        M._ensure_weight_filename(tmp)
        check(
            "no alias created when more than one candidate file exists",
            not (Path(tmp) / "weights.safetensors").exists(),
        )


# ---------------------------------------------------------------------------
# Interop: EngineSegment -> from_engine_segment (the canonical transcript shape)
# ---------------------------------------------------------------------------

def test_engine_segment_interoperates_with_transcript_schema():
    seg = M.EngineSegment(
        start=10.0, end=12.5, text=" a quick test",
        words=[
            M.EngineWord(word="a", start=10.0, end=10.2, probability=0.99),
            M.EngineWord(word="quick", start=10.2, end=10.6, probability=0.97),
            M.EngineWord(word="test", start=10.6, end=12.5, probability=0.93),
        ],
        tokens=[1, 2, 3],
        avg_logprob=-0.15, no_speech_prob=0.02, compression_ratio=1.3, temperature=None,
    )
    doc_seg = from_engine_segment(0, seg)
    check("id set", doc_seg["id"] == 0)
    check("start carried through", doc_seg["start"] == 10.0)
    check("end carried through", doc_seg["end"] == 12.5)
    check("text stripped", doc_seg["text"] == "a quick test")
    check("word count", len(doc_seg["words"]) == 3)
    check("word text stripped", doc_seg["words"][0]["w"] == "a")
    check("word timing", doc_seg["words"][2]["end"] == 12.5)
    check("avg_logprob rounded", doc_seg["avg_logprob"] == -0.15)
    check("temperature None is tolerated", doc_seg["temperature"] is None)


def test_engine_segment_defaults_are_safe_for_transcript_schema():
    # A segment with no words at all (silence, or a chunk mlx skipped).
    seg = M.EngineSegment(start=0.0, end=1.0, text="")
    doc_seg = from_engine_segment(0, seg)
    check("empty words list tolerated", doc_seg["words"] == [])
    check("empty text tolerated", doc_seg["text"] == "")


# ---------------------------------------------------------------------------
# engine.py: device resolution on fabricated hardware
# ---------------------------------------------------------------------------

import app.engine as _engine_module
_ORIGINAL_SETTINGS_GET = _engine_module.SETTINGS.get  # captured once, pristine


def _engine_with_hardware(hw: dict, device_setting: str) -> WhisperEngine:
    eng = WhisperEngine()
    eng._hardware = hw
    eng.hardware = lambda refresh=False: hw  # avoid catalog.probe_hardware()

    # Always wrap the pristine original, never a previous test's wrapper -
    # otherwise repeated calls nest indefinitely and never truly reset.
    _engine_module.SETTINGS.get = lambda key, default=None: (
        device_setting if key == "device" else _ORIGINAL_SETTINGS_GET(key, default)
    )
    return eng


def test_auto_prefers_cuda_over_mlx_when_both_usable():
    hw = {"cuda_usable": True, "apple_silicon": True, "mlx_available": True}
    eng = _engine_with_hardware(hw, "auto")
    device, index = eng.resolve_device()
    check("auto prefers cuda when both are usable", device == "cuda", device)


def test_auto_prefers_mlx_on_apple_silicon_without_cuda():
    hw = {"cuda_usable": False, "apple_silicon": True, "mlx_available": True}
    eng = _engine_with_hardware(hw, "auto")
    device, index = eng.resolve_device()
    check("auto picks mlx on Apple Silicon", device == "mlx", device)


def test_auto_falls_back_to_cpu_when_mlx_unavailable():
    hw = {"cuda_usable": False, "apple_silicon": True, "mlx_available": False}
    eng = _engine_with_hardware(hw, "auto")
    device, index = eng.resolve_device()
    check("auto falls back to cpu without mlx-whisper installed", device == "cpu", device)


def test_explicit_mlx_falls_back_to_cpu_when_unavailable():
    hw = {"cuda_usable": False, "apple_silicon": True, "mlx_available": False}
    eng = _engine_with_hardware(hw, "mlx")
    device, index = eng.resolve_device()
    check("explicit 'mlx' falls back to cpu when unavailable", device == "cpu", device)


def test_explicit_mlx_honoured_when_available():
    hw = {"cuda_usable": False, "apple_silicon": True, "mlx_available": True}
    eng = _engine_with_hardware(hw, "mlx")
    device, index = eng.resolve_device()
    check("explicit 'mlx' honoured when available", device == "mlx", device)


def test_compute_type_for_mlx_is_fixed_sentinel():
    eng = WhisperEngine()
    check("mlx compute type is the fixed sentinel", eng.resolve_compute_type("mlx") == "mlx-native")


# ---------------------------------------------------------------------------
# engine.py: model/backend mismatch reconciliation (regression coverage for
# the real bug this was written to fix: a CTranslate2-only model like
# "medium" selected while Device is "auto" and resolves to "mlx" on Apple
# Silicon crashed deep inside mlx_whisper's model loader with a confusing
# "ModelDimensions.__init__() got an unexpected keyword argument
# 'alignment_heads'" error, because faster-whisper's config.json schema is
# not mlx_whisper's. _reconcile_backend() must catch this before either
# loader ever sees the mismatched model.
# ---------------------------------------------------------------------------

def test_reconcile_downgrades_ctranslate2_model_away_from_mlx_device():
    eng = WhisperEngine()
    eng._hardware = {"apple_silicon": True, "mlx_available": True, "cuda_usable": False}
    eng.hardware = lambda refresh=False: eng._hardware
    spec = catalog.model_spec("medium")
    device, index = eng._reconcile_backend(spec, "medium", "mlx", None)
    check(
        "a CTranslate2 model forced off the mlx device falls back to cpu",
        device == "cpu" and index is None, device,
    )


def test_reconcile_downgrades_to_cuda_when_usable():
    eng = WhisperEngine()
    eng._hardware = {"apple_silicon": True, "mlx_available": True, "cuda_usable": True}
    eng.hardware = lambda refresh=False: eng._hardware
    spec = catalog.model_spec("medium")
    device, index = eng._reconcile_backend(spec, "medium", "mlx", None)
    check(
        "a CTranslate2 model forced off the mlx device prefers cuda if usable",
        device == "cuda", device,
    )


def test_reconcile_forces_mlx_model_onto_mlx_device():
    eng = WhisperEngine()
    eng._hardware = {"apple_silicon": True, "mlx_available": True, "cuda_usable": False}
    eng.hardware = lambda refresh=False: eng._hardware
    spec = catalog.model_spec("tiny-mlx")
    device, index = eng._reconcile_backend(spec, "tiny-mlx", "cpu", None)
    check(
        "an MLX-only model forces the device to mlx regardless of setting",
        device == "mlx" and index is None, device,
    )


def test_reconcile_raises_when_mlx_model_selected_but_mlx_unavailable():
    eng = WhisperEngine()
    eng._hardware = {"apple_silicon": True, "mlx_available": False, "cuda_usable": False}
    eng.hardware = lambda refresh=False: eng._hardware
    spec = catalog.model_spec("tiny-mlx")
    try:
        eng._reconcile_backend(spec, "tiny-mlx", "cpu", None)
        check("raises EngineError when MLX model selected but unavailable", False)
    except EngineError:
        check("raises EngineError when MLX model selected but unavailable", True)


def test_reconcile_is_a_noop_when_model_and_device_already_agree():
    eng = WhisperEngine()
    eng._hardware = {"apple_silicon": True, "mlx_available": True, "cuda_usable": False}
    eng.hardware = lambda refresh=False: eng._hardware
    spec = catalog.model_spec("medium")
    device, index = eng._reconcile_backend(spec, "medium", "cpu", None)
    check("ctranslate2 model already on cpu is left alone", device == "cpu" and index is None)

    spec_mlx = catalog.model_spec("tiny-mlx")
    device, index = eng._reconcile_backend(spec_mlx, "tiny-mlx", "mlx", None)
    check("mlx model already on mlx is left alone", device == "mlx" and index is None)


# ---------------------------------------------------------------------------
# catalog.py: recommendation on fabricated Apple Silicon hardware
# ---------------------------------------------------------------------------

def test_recommend_large_memory_mac():
    hw = {"apple_silicon": True, "mlx_available": True, "unified_memory_gb": 64,
          "cuda_usable": False, "cpu_cores": 10}
    rec = catalog.recommend(hw)
    check("64GB Mac recommends full-precision large-v3-mlx", rec["model"] == "large-v3-mlx", rec["model"])
    check("device is mlx", rec["device"] == "mlx")


def test_recommend_mid_memory_mac():
    hw = {"apple_silicon": True, "mlx_available": True, "unified_memory_gb": 18,
          "cuda_usable": False, "cpu_cores": 8}
    rec = catalog.recommend(hw)
    check("18GB Mac recommends 4-bit turbo", rec["model"] == "large-v3-turbo-mlx-4bit", rec["model"])


def test_recommend_low_memory_mac():
    hw = {"apple_silicon": True, "mlx_available": True, "unified_memory_gb": 8,
          "cuda_usable": False, "cpu_cores": 8}
    rec = catalog.recommend(hw)
    check("8GB Mac recommends small-mlx", rec["model"] == "small-mlx", rec["model"])


def test_recommend_apple_silicon_without_mlx_falls_back_to_cpu_branch():
    hw = {"apple_silicon": True, "mlx_available": False, "unified_memory_gb": 64,
          "cuda_usable": False, "cpu_cores": 10}
    rec = catalog.recommend(hw)
    check("no mlx-whisper falls back to the CPU core-count branch", rec["device"] == "cpu", rec["device"])


def main() -> int:
    tests = [
        test_translate_params_keeps_known_keys_drops_unknown,
        test_segments_from_result_offsets_timestamps,
        test_ensure_weight_filename_aliases_a_single_oddly_named_file,
        test_ensure_weight_filename_is_a_noop_when_expected_name_present,
        test_ensure_weight_filename_is_a_noop_when_ambiguous,
        test_engine_segment_interoperates_with_transcript_schema,
        test_engine_segment_defaults_are_safe_for_transcript_schema,
        test_auto_prefers_cuda_over_mlx_when_both_usable,
        test_auto_prefers_mlx_on_apple_silicon_without_cuda,
        test_auto_falls_back_to_cpu_when_mlx_unavailable,
        test_explicit_mlx_falls_back_to_cpu_when_unavailable,
        test_explicit_mlx_honoured_when_available,
        test_compute_type_for_mlx_is_fixed_sentinel,
        test_reconcile_downgrades_ctranslate2_model_away_from_mlx_device,
        test_reconcile_downgrades_to_cuda_when_usable,
        test_reconcile_forces_mlx_model_onto_mlx_device,
        test_reconcile_raises_when_mlx_model_selected_but_mlx_unavailable,
        test_reconcile_is_a_noop_when_model_and_device_already_agree,
        test_recommend_large_memory_mac,
        test_recommend_mid_memory_mac,
        test_recommend_low_memory_mac,
        test_recommend_apple_silicon_without_mlx_falls_back_to_cpu_branch,
    ]
    for test in tests:
        print(f"\n{test.__name__}")
        test()
    _engine_module.SETTINGS.get = _ORIGINAL_SETTINGS_GET
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
