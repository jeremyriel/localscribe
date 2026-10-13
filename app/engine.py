"""The Whisper engine: model lifecycle, transcription, and throughput stats.

Transcription runs on faster-whisper, which executes genuine OpenAI Whisper
weights on the CTranslate2 runtime. The model is loaded lazily and kept
resident between jobs, because loading Large v3 costs several seconds and a
researcher transcribing a folder of interviews should pay that once.

Everything that takes time reports to the console as it happens, and every
segment updates a rolling throughput estimate so the ETA converges instead of
swinging wildly on the first few segments.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from . import catalog, languages
from .config import SETTINGS, parse_temperature_ladder
from .console import CONSOLE, fmt_duration

# Rolling window over which throughput is averaged for the ETA. Short enough to
# react to a slow patch of audio, long enough to stop the estimate jittering.
RATE_WINDOW = 12


class EngineError(RuntimeError):
    """Model could not be loaded or transcription could not start."""


class Cancelled(Exception):
    """Raised inside the transcription loop when the job is cancelled."""


def _is_missing_gpu_library(exc: BaseException) -> bool:
    """True for CTranslate2's 'cublas64_12.dll is not found' family of errors."""
    text = str(exc).lower()
    if "cublas" in text or "cudnn" in text:
        return True
    return ".dll" in text and ("not found" in text or "cannot be loaded" in text
                               or "could not locate" in text)


# ---------------------------------------------------------------------------
# Throughput tracking
# ---------------------------------------------------------------------------


@dataclass
class RateTracker:
    """Measures audio-seconds, words and tokens per wall-clock second."""

    total_audio: float = 0.0
    started: float = field(default_factory=time.monotonic)
    audio_done: float = 0.0
    words: int = 0
    tokens: int = 0
    segments: int = 0
    _window: deque = field(default_factory=lambda: deque(maxlen=RATE_WINDOW))

    def update(self, audio_position: float, words: int, tokens: int) -> None:
        now = time.monotonic()
        self.audio_done = max(self.audio_done, float(audio_position))
        self.words += int(words)
        self.tokens += int(tokens)
        self.segments += 1
        self._window.append((now, self.audio_done))

    @property
    def elapsed(self) -> float:
        return max(1e-6, time.monotonic() - self.started)

    @property
    def realtime_factor(self) -> float:
        """Audio seconds processed per wall-clock second (higher is faster)."""
        return self.audio_done / self.elapsed

    @property
    def recent_realtime_factor(self) -> float:
        """Realtime factor over the rolling window, for a stable ETA."""
        if len(self._window) < 2:
            return self.realtime_factor
        (t0, a0), (t1, a1) = self._window[0], self._window[-1]
        span = t1 - t0
        if span <= 0.05:
            return self.realtime_factor
        return max(1e-6, (a1 - a0) / span)

    @property
    def words_per_second(self) -> float:
        return self.words / self.elapsed

    @property
    def tokens_per_second(self) -> float:
        return self.tokens / self.elapsed

    @property
    def progress(self) -> float:
        if self.total_audio <= 0:
            return 0.0
        return min(1.0, self.audio_done / self.total_audio)

    @property
    def eta(self) -> float | None:
        """Seconds remaining, or None until an estimate is meaningful."""
        if self.total_audio <= 0:
            return None
        remaining = self.total_audio - self.audio_done
        if remaining <= 0:
            return 0.0
        # Fewer than two samples, or under three seconds of work, is not enough
        # to estimate from; showing a wild number is worse than showing none.
        if len(self._window) < 2 or self.elapsed < 3.0:
            return None
        return remaining / self.recent_realtime_factor

    def snapshot(self) -> dict:
        return {
            "progress": round(self.progress, 4),
            "audio_done": round(self.audio_done, 2),
            "audio_total": round(self.total_audio, 2),
            "elapsed": round(self.elapsed, 1),
            "eta": (round(self.eta, 1) if self.eta is not None else None),
            "eta_text": (fmt_duration(self.eta) if self.eta is not None else "estimating"),
            "realtime_factor": round(self.realtime_factor, 2),
            "words": self.words,
            "words_per_second": round(self.words_per_second, 2),
            "tokens": self.tokens,
            "tokens_per_second": round(self.tokens_per_second, 2),
            "segments": self.segments,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class WhisperEngine:
    """Owns the loaded model and translates settings into engine parameters."""

    def __init__(self) -> None:
        self._model = None
        self._signature: tuple | None = None
        self._lock = threading.RLock()
        self._hardware: dict | None = None
        self._state = "idle"          # idle | loading | ready | error
        self._detail = "No model loaded yet"
        self._loaded_model = ""
        self._loaded_device = ""
        self._loaded_compute = ""
        self._backend = "ctranslate2"   # ctranslate2 | mlx
        self._load_seconds = 0.0
        # Set when CUDA failed mid-transcription. CTranslate2 keeps its
        # library state for the life of the process, so CUDA stays off until
        # Local Scribe restarts even if a later probe looks healthy.
        self.cuda_runtime_failed = False

    # -- hardware ----------------------------------------------------------

    def hardware(self, refresh: bool = False) -> dict:
        if self._hardware is None or refresh:
            self._hardware = catalog.probe_hardware()
            if self.cuda_runtime_failed:
                self._hardware["cuda_usable"] = False
        return self._hardware

    def resolve_device(self) -> tuple[str, int | None]:
        """Return (device, device_index) honouring the setting and reality."""
        wanted = str(SETTINGS.get("device", "auto") or "auto").strip()
        hw = self.hardware()

        if wanted.startswith("cuda"):
            if not hw.get("cuda_usable"):
                return "cpu", None
            index = None
            if ":" in wanted:
                try:
                    index = int(wanted.split(":", 1)[1])
                except ValueError:
                    index = None
            return "cuda", index

        if wanted == "mlx":
            if hw.get("apple_silicon") and hw.get("mlx_available"):
                return "mlx", None
            CONSOLE.warn(
                "Device is set to 'mlx' but the MLX runtime is not available "
                "on this machine; falling back to CPU. Install mlx-whisper "
                "(it ships in requirements.txt on Apple Silicon) or set "
                "Device to 'auto'.",
            )
            return "cpu", None

        if wanted == "cpu":
            return "cpu", None

        # auto: prefer a GPU path when one is actually usable, in order of
        # how much faster it is over CPU; otherwise CPU.
        if hw.get("cuda_usable"):
            return "cuda", None
        if hw.get("apple_silicon") and hw.get("mlx_available"):
            return "mlx", None
        return "cpu", None

    def resolve_compute_type(self, device: str) -> str:
        if device == "mlx":
            # Quantization on MLX is baked into which model repo is chosen
            # (see catalog.py), not a runtime precision flag - this is a
            # fixed, informational value for logging/telemetry only.
            return "mlx-native"

        wanted = str(SETTINGS.get("compute_type", "auto") or "auto").strip()
        hw = self.hardware()

        if wanted != "auto":
            supported = hw.get("cuda_compute_types" if device == "cuda" else "cpu_compute_types") or []
            if supported and wanted not in supported:
                CONSOLE.warn(
                    f"Compute type '{wanted}' is not supported on this "
                    f"{device.upper()}; falling back to automatic selection.",
                    supported=supported,
                )
            else:
                return wanted

        if device == "cuda":
            gpus = hw.get("gpus") or []
            if gpus and catalog._pascal_or_older(gpus[0]):
                # Pascal exposes fp16 at 1/64 rate; int8 is dramatically faster.
                return "int8_float32"
            return "float16"

        supported = hw.get("cpu_compute_types") or []
        for candidate in ("int8", "int8_float32", "float32"):
            if candidate in supported:
                return candidate
        return "int8"

    # -- state -------------------------------------------------------------

    def status(self) -> dict:
        hw = self.hardware()
        device, index = self.resolve_device()
        model_key = str(SETTINGS.get("model") or "")
        spec = catalog.model_spec(model_key)
        return {
            "state": self._state,
            "detail": self._detail,
            "ready": self._state == "ready",
            "selected_model": model_key,
            "selected_model_label": spec.label if spec else model_key,
            "model_downloaded": catalog.is_downloaded(model_key),
            "loaded_model": self._loaded_model,
            "loaded_device": self._loaded_device,
            "loaded_compute": self._loaded_compute,
            "load_seconds": round(self._load_seconds, 2),
            "target_device": device + (f":{index}" if index is not None else ""),
            "target_compute": self.resolve_compute_type(device),
            "cuda_usable": bool(hw.get("cuda_usable")),
            "gpu_count": len(hw.get("gpus") or []),
            "gpu_name": (hw.get("gpus") or [""])[0],
            "driver_version": hw.get("driver_version"),
            "cpu_cores": hw.get("cpu_cores"),
            "backend": self._backend,
            "apple_silicon": bool(hw.get("apple_silicon")),
            "mlx_available": bool(hw.get("mlx_available")),
            "unified_memory_gb": hw.get("unified_memory_gb"),
            "warnings": hw.get("warnings") or [],
            "notes": hw.get("notes") or [],
            "offline_lock": bool(SETTINGS.get("offline_lock")),
        }

    # -- loading -----------------------------------------------------------

    def _build_signature(self, model_key: str, device: str, index, compute: str) -> tuple:
        return (
            model_key, device, index, compute,
            int(SETTINGS.get("cpu_threads") or 0),
            int(SETTINGS.get("num_workers") or 1),
        )

    def _reconcile_backend(
        self, spec, model_key: str, device: str, index: int | None,
    ) -> tuple[str, int | None]:
        """Resolve a mismatch between the selected model and the resolved device.

        The selected model's own file format determines which backend can
        actually load it: an MLX model's weights (*.safetensors/*.npz)
        cannot be read by CTranslate2's WhisperModel, and a CTranslate2
        model's config.json cannot be read by mlx_whisper - it uses a
        completely different schema (e.g. faster-whisper's config.json
        carries an "alignment_heads" key that mlx_whisper's ModelDimensions
        does not accept, so loading one through the wrong backend fails deep
        inside the loader with a confusing error instead of a clear one).

        `resolve_device()` only reflects hardware/settings, not which model
        is currently selected, so the two can disagree - most commonly when
        Device is 'auto' and the selected model predates MLX being
        available on this machine. This reconciles that before either
        loader runs, raising EngineError only when there is truly no way to
        load the selected model at all.
        """
        if spec.engine == "mlx" and device != "mlx":
            hw = self.hardware()
            if hw.get("apple_silicon") and hw.get("mlx_available"):
                CONSOLE.warn(
                    f"{spec.label} only runs via the Apple GPU (MLX) "
                    "backend; using it instead of the configured Device "
                    "setting.",
                    model=model_key, configured_device=device,
                )
                return "mlx", None
            raise EngineError(
                f"{spec.label} is an Apple GPU (MLX) model and needs "
                "MLX, which is not available on this machine. Choose a "
                "different model in Settings, or install mlx-whisper "
                "(pip install -r requirements.txt) on Apple Silicon."
            )

        if spec.engine != "mlx" and device == "mlx":
            fallback = "cuda" if self.hardware().get("cuda_usable") else "cpu"
            CONSOLE.warn(
                f"{spec.label} does not support the Apple GPU (MLX) "
                f"backend; running on {fallback.upper()} instead. Choose a "
                "model labelled '(Apple GPU)' in Settings to use MLX.",
                model=model_key,
            )
            return fallback, None

        return device, index

    def ensure_loaded(self, force: bool = False):
        """Load the configured model, reusing it when nothing relevant changed."""
        model_key = str(SETTINGS.get("model") or "").strip()
        spec = catalog.model_spec(model_key)
        if spec is None:
            raise EngineError(
                f"Model '{model_key}' is not in the catalogue. Choose a model "
                "in Settings."
            )

        snapshot = catalog.local_model_path(model_key)
        if snapshot is None:
            raise EngineError(
                f"{spec.label} has not been downloaded yet. Open Settings "
                f"and download it ({spec.download_mb} MB), then try again."
            )

        device, index = self.resolve_device()
        device, index = self._reconcile_backend(spec, model_key, device, index)
        compute = self.resolve_compute_type(device)
        signature = self._build_signature(model_key, device, index, compute)

        with self._lock:
            if self._model is not None and signature == self._signature and not force:
                return self._model

            if self._model is not None:
                CONSOLE.info(
                    "Engine settings changed; reloading the model.",
                    was=self._loaded_model, now=model_key,
                )
                self._model = None
                self._signature = None

            self._state = "loading"
            self._detail = f"Loading {spec.label} on {device.upper()} ({compute})"
            CONSOLE.step(
                f"Loading {spec.label} ({spec.params} parameters) on "
                f"{device.upper()} with {compute} precision",
                model=model_key, device=device, compute_type=compute,
                path=str(snapshot),
            )

            started = time.monotonic()

            if device == "mlx":
                from . import engine_mlx
                try:
                    engine_mlx.load_model(str(snapshot))
                except engine_mlx.MlxLoadError as exc:
                    self._state = "error"
                    self._detail = str(exc)
                    CONSOLE.error(f"Model failed to load: {exc}", model=model_key)
                    raise EngineError(self._explain_load_failure(exc, device, compute)) from exc
                # MLX has no persistent "model" handle worth keeping - it
                # caches by repo/path internally. The resolved snapshot path
                # doubles as the cache key transcribe() passes back in.
                model = snapshot
            else:
                from faster_whisper import WhisperModel

                kwargs = {
                    "device": device,
                    "compute_type": compute,
                    "num_workers": max(1, int(SETTINGS.get("num_workers") or 1)),
                    "local_files_only": True,
                }
                if device == "cpu":
                    threads = int(SETTINGS.get("cpu_threads") or 0)
                    if threads > 0:
                        kwargs["cpu_threads"] = threads
                if index is not None:
                    kwargs["device_index"] = index

                try:
                    model = WhisperModel(str(snapshot), **kwargs)
                except Exception as exc:
                    self._state = "error"
                    self._detail = str(exc)
                    CONSOLE.error(f"Model failed to load: {exc}", model=model_key)
                    raise EngineError(self._explain_load_failure(exc, device, compute)) from exc

            self._load_seconds = time.monotonic() - started
            self._model = model
            self._signature = signature
            self._state = "ready"
            self._loaded_model = model_key
            self._loaded_device = device + (f":{index}" if index is not None else "")
            self._loaded_compute = compute
            self._backend = "mlx" if device == "mlx" else "ctranslate2"
            self._detail = (
                f"{spec.label} ready on {self._loaded_device.upper()} ({compute})"
            )
            CONSOLE.success(
                f"{spec.label} ready in {self._load_seconds:.1f}s on "
                f"{self._loaded_device.upper()} ({compute})",
                load_seconds=round(self._load_seconds, 2),
            )
            return self._model

    def _explain_load_failure(self, exc: Exception, device: str, compute: str) -> str:
        text = str(exc)
        lowered = text.lower()
        if device == "mlx":
            return (
                "The model could not load via MLX (Apple GPU). Set Device to "
                "'cpu' in Settings to continue without the GPU, or check "
                "that mlx-whisper installed correctly "
                "(pip install -r requirements.txt). "
                f"Original error: {text}"
            )
        if device == "cuda" and _is_missing_gpu_library(exc):
            return (
                "The model could not load on the GPU because a CUDA support "
                "library is missing. Use 'Install GPU support' in Settings, "
                "or set Device to CPU to continue without the GPU. "
                f"Original error: {text}"
            )
        if device == "cuda" and "driver" in lowered:
            return (
                "The model could not load on the GPU because the NVIDIA driver "
                "is older than the CUDA 12 runtime requires. Update the driver "
                "or set Device to CPU. "
                f"Original error: {text}"
            )
        if ".dll" in lowered or "dll load failed" in lowered:
            return (
                "The model could not load because a Windows system library is "
                "missing. Install the Microsoft Visual C++ 2015-2022 "
                "Redistributable (x64) from "
                "https://aka.ms/vs/17/release/vc_redist.x64.exe and restart "
                f"Local Scribe. Original error: {text}"
            )
        if "unsupported" in lowered and "compute" in lowered:
            return (
                f"Compute type '{compute}' is not supported on this hardware. "
                "Set Compute type back to 'auto' in Settings. "
                f"Original error: {text}"
            )
        return f"The model could not be loaded: {text}"

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self._signature = None
            self._state = "idle"
            self._detail = "Model unloaded"
            self._loaded_model = ""

    # -- transcription -----------------------------------------------------

    def transcribe_params(self, overrides: dict | None = None) -> dict:
        """Translate settings into faster-whisper transcribe() keywords."""
        s = SETTINGS.all()
        language = languages.coerce(s.get("language"))

        params = {
            "task": s.get("task") or "transcribe",
            "beam_size": int(s.get("beam_size") or 5),
            "best_of": int(s.get("best_of") or 5),
            "patience": float(s.get("patience") or 1.0),
            "length_penalty": float(s.get("length_penalty") or 1.0),
            "repetition_penalty": float(s.get("repetition_penalty") or 1.0),
            "no_repeat_ngram_size": int(s.get("no_repeat_ngram_size") or 0),
            "temperature": parse_temperature_ladder(s.get("temperature_fallback")),
            "compression_ratio_threshold": float(s.get("compression_ratio_threshold") or 2.4),
            "log_prob_threshold": float(s.get("log_prob_threshold") or -1.0),
            "no_speech_threshold": float(s.get("no_speech_threshold") or 0.6),
            "condition_on_previous_text": bool(s.get("condition_on_previous_text")),
            "prompt_reset_on_temperature": float(s.get("prompt_reset_on_temperature") or 0.5),
            "chunk_length": int(s.get("chunk_length") or 30),
            "multilingual": bool(s.get("multilingual")),
            # Word timestamps are non-negotiable: the validation editor, the
            # caption files and the re-timestamp pass all depend on them.
            "word_timestamps": True,
            "vad_filter": bool(s.get("vad_filter")),
        }

        if language != languages.AUTO:
            params["language"] = language

        max_new = int(s.get("max_new_tokens") or 0)
        if max_new > 0:
            params["max_new_tokens"] = max_new

        hallucination = float(s.get("hallucination_silence_threshold") or 0.0)
        if hallucination > 0:
            params["hallucination_silence_threshold"] = hallucination

        prompt = (s.get("initial_prompt") or "").strip()
        if prompt:
            params["initial_prompt"] = prompt
        hotwords = (s.get("hotwords") or "").strip()
        if hotwords:
            params["hotwords"] = hotwords

        if params["vad_filter"]:
            params["vad_parameters"] = {
                "threshold": float(s.get("vad_threshold") or 0.5),
                "min_speech_duration_ms": int(s.get("vad_min_speech_duration_ms") or 250),
                "min_silence_duration_ms": int(s.get("vad_min_silence_duration_ms") or 2000),
                "speech_pad_ms": int(s.get("vad_speech_pad_ms") or 400),
            }

        # The advanced panel forwards anything not surfaced in the UI, so a new
        # faster-whisper parameter never requires a code change here.
        advanced = s.get("advanced_raw") or {}
        if isinstance(advanced, dict):
            params.update(advanced)

        if overrides:
            params.update(overrides)

        # "auto" is not a Whisper language: it means leave `language` out so
        # the decoder detects it. A per-run override of "auto" must also be
        # able to cancel a language set by the saved settings or the advanced
        # panel. Any other value is normalised to a bare code; an unrecognised
        # one passes through untouched, since the advanced panel is a raw
        # passthrough by design.
        if "language" in params:
            parsed = languages.parse(params["language"])
            if parsed == languages.AUTO:
                params.pop("language")
            elif parsed:
                params["language"] = parsed

        return params

    def transcribe(
        self,
        audio,
        *,
        duration: float,
        overrides: dict | None = None,
        on_segment=None,
        should_cancel=None,
    ) -> dict:
        """Transcribe decoded audio, reporting progress as segments arrive.

        ``audio`` is a float32 mono 16 kHz numpy array. Passing decoded samples
        rather than a path means the media layer owns all format handling and
        the engine never needs to know what a .mkv is.

        If the GPU turns out to be missing a CUDA library at the first
        computation (CTranslate2 loads cuBLAS/cuDNN lazily, so this can only
        surface here), the job is retried once on CPU instead of failing.
        """
        emitted = 0

        def counting(index, segment, tracker):
            nonlocal emitted
            emitted += 1
            if on_segment is not None:
                on_segment(index, segment, tracker)

        try:
            return self._transcribe_once(
                audio, duration=duration, overrides=overrides,
                on_segment=counting, should_cancel=should_cancel,
            )
        except Cancelled:
            raise
        except Exception as exc:
            if not (
                self._backend == "ctranslate2"
                and self._loaded_device.startswith("cuda")
                and emitted == 0
                and _is_missing_gpu_library(exc)
            ):
                if not isinstance(exc, EngineError) and "dll" in str(exc).lower():
                    # e.g. onnxruntime (voice activity filter) without the
                    # Visual C++ runtime: say what to install, not just why.
                    raise EngineError(
                        self._explain_load_failure(exc, self._loaded_device, self._loaded_compute)
                    ) from exc
                raise
            self._disable_cuda(exc)

        return self._transcribe_once(
            audio, duration=duration, overrides=overrides,
            on_segment=on_segment, should_cancel=should_cancel,
        )

    def _disable_cuda(self, exc: Exception) -> None:
        """Stop using CUDA for the rest of this session after a runtime failure."""
        self.cuda_runtime_failed = True
        hw = self.hardware()
        hw["cuda_usable"] = False
        warning = (
            "The GPU is missing a CUDA support library (cuBLAS 12 or cuDNN 9), "
            "so transcription has switched to CPU for this session. Use "
            "'Install GPU support' in Settings, then restart Local Scribe, "
            "to use the GPU. "
            f"Original error: {exc}"
        )
        if warning not in hw.setdefault("warnings", []):
            hw["warnings"].append(warning)
        CONSOLE.warn(warning)
        self.unload()
        CONSOLE.step("Retrying transcription on CPU")

    def _transcribe_once(
        self,
        audio,
        *,
        duration: float,
        overrides: dict | None = None,
        on_segment=None,
        should_cancel=None,
    ) -> dict:
        model = self.ensure_loaded()
        params = self.transcribe_params(overrides)

        loggable = {
            k: (list(v) if isinstance(v, tuple) else v)
            for k, v in params.items()
            if k not in ("vad_parameters",)
        }
        CONSOLE.step(
            "Starting translation to English" if loggable.get("task") == "translate"
            else "Starting transcription",
            **{
            "beam_size": loggable.get("beam_size"),
            "language": loggable.get("language", "auto-detect"),
            "task": loggable.get("task"),
            "vad": params.get("vad_filter"),
            "temperature_ladder": loggable.get("temperature"),
        })
        CONSOLE.debug("Full engine parameters", params=loggable)

        tracker = RateTracker(total_audio=float(duration))
        started = time.monotonic()

        if self._backend == "mlx":
            from . import engine_mlx
            segments_iter, info = engine_mlx.transcribe(
                audio, model_repo=str(model), params=params, should_cancel=should_cancel,
            )
        else:
            segments_iter, info = model.transcribe(audio, **params)

        detected = getattr(info, "language", "") or ""
        probability = float(getattr(info, "language_probability", 0.0) or 0.0)
        if "language" not in params:
            CONSOLE.info(
                f"Detected language: {languages.label(detected) or 'unknown'} "
                f"({probability * 100:.1f}% confidence)",
                language=detected, probability=round(probability, 4),
            )
        else:
            # A forced language is the language of the transcript even if a
            # back end reports nothing back about it.
            detected = detected or str(params["language"])
            CONSOLE.info(
                f"Language set to {languages.label(params['language'])}",
                language=params["language"],
            )

        vad_duration = float(getattr(info, "duration_after_vad", 0.0) or 0.0)
        if params.get("vad_filter") and vad_duration and duration:
            trimmed = duration - vad_duration
            if trimmed > 1.0:
                CONSOLE.info(
                    f"Voice activity detection trimmed {fmt_duration(trimmed)} "
                    f"of silence, leaving {fmt_duration(vad_duration)} of speech "
                    "to transcribe",
                    trimmed_seconds=round(trimmed, 1),
                    speech_seconds=round(vad_duration, 1),
                )
            # The ETA must be based on the audio actually fed to the decoder.
            tracker.total_audio = vad_duration

        collected: list = []
        for index, segment in enumerate(segments_iter):
            if should_cancel is not None and should_cancel():
                CONSOLE.warn("Cancellation requested; stopping transcription.")
                raise Cancelled()

            words = getattr(segment, "words", None) or []
            token_count = len(getattr(segment, "tokens", None) or [])
            word_count = len(words) or len((segment.text or "").split())

            tracker.update(float(segment.end or 0.0), word_count, token_count)
            collected.append(segment)

            if on_segment is not None:
                on_segment(index, segment, tracker)

        elapsed = time.monotonic() - started
        stats = tracker.snapshot()
        stats.update({
            "wall_seconds": round(elapsed, 2),
            "audio_seconds": round(float(duration), 2),
            "speech_seconds": round(vad_duration, 2) if vad_duration else None,
            "model": self._loaded_model,
            "device": self._loaded_device,
            "compute_type": self._loaded_compute,
            "language": detected,
            "language_probability": round(probability, 4),
        })

        return {
            "segments": collected,
            "info": info,
            "language": detected,
            "language_probability": probability,
            "language_requested": params.get("language") or languages.AUTO,
            "stats": stats,
            "params": loggable,
        }


ENGINE = WhisperEngine()
