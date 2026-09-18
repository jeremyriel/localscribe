"""Whisper model catalogue, hardware probing, and local model management.

The catalogue is the honest part of the settings page. Each entry carries what
a researcher actually needs to choose: how large the download is, roughly how
fast it runs, and what it is and is not suitable for. Recommendations are
computed from detected hardware and always state their reason, because a
recommendation without a reason is just a default.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

from .config import PATHS

# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    key: str
    repo: str
    label: str
    params: str
    download_mb: int
    vram_gb: float
    speed: str            # relative throughput, plain language
    languages: str        # "multilingual" or "English only"
    suitability: str      # what it is good for, and what it is not
    tier: str             # draft | working | research
    engine: str = "ctranslate2"   # ctranslate2 | mlx


CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec(
        key="tiny", repo="Systran/faster-whisper-tiny",
        label="Tiny", params="39M", download_mb=75, vram_gb=1.0,
        speed="fastest (~30x realtime on GPU)", languages="multilingual",
        suitability=(
            "Smoke-testing the pipeline and keyword spotting. Word error rates "
            "are high enough that it should not be used for research "
            "transcripts, even as a first pass."
        ),
        tier="draft",
    ),
    ModelSpec(
        key="tiny.en", repo="Systran/faster-whisper-tiny.en",
        label="Tiny (English)", params="39M", download_mb=75, vram_gb=1.0,
        speed="fastest", languages="English only",
        suitability="As Tiny, slightly better on English. Still draft quality only.",
        tier="draft",
    ),
    ModelSpec(
        key="base", repo="Systran/faster-whisper-base",
        label="Base", params="74M", download_mb=145, vram_gb=1.0,
        speed="very fast (~16x realtime on GPU)", languages="multilingual",
        suitability=(
            "Quick rough drafts. Noticeably error-prone on proper nouns, "
            "overlapping speech and accents."
        ),
        tier="draft",
    ),
    ModelSpec(
        key="base.en", repo="Systran/faster-whisper-base.en",
        label="Base (English)", params="74M", download_mb=145, vram_gb=1.0,
        speed="very fast", languages="English only",
        suitability="As Base, better on English. Draft quality.",
        tier="draft",
    ),
    ModelSpec(
        key="small", repo="Systran/faster-whisper-small",
        label="Small", params="244M", download_mb=480, vram_gb=2.0,
        speed="fast (~6x realtime on GPU)", languages="multilingual",
        suitability=(
            "A reasonable floor for clean, single-speaker, close-microphone "
            "audio. Expect to correct names and technical terms by hand."
        ),
        tier="working",
    ),
    ModelSpec(
        key="small.en", repo="Systran/faster-whisper-small.en",
        label="Small (English)", params="244M", download_mb=480, vram_gb=2.0,
        speed="fast", languages="English only",
        suitability="As Small, better on English-only material.",
        tier="working",
    ),
    ModelSpec(
        key="distil-small.en", repo="Systran/faster-distil-whisper-small.en",
        label="Distil Small (English)", params="166M", download_mb=330, vram_gb=1.5,
        speed="very fast", languages="English only",
        suitability=(
            "Distilled for speed on modest hardware. A good CPU-only choice "
            "when Medium is too slow to be practical."
        ),
        tier="working",
    ),
    ModelSpec(
        key="medium", repo="Systran/faster-whisper-medium",
        label="Medium", params="769M", download_mb=1530, vram_gb=5.0,
        speed="moderate (~2x realtime on GPU)", languages="multilingual",
        suitability=(
            "Good accuracy and the most practical choice when running on CPU "
            "only. A sound default for interview audio of decent quality."
        ),
        tier="research",
    ),
    ModelSpec(
        key="medium.en", repo="Systran/faster-whisper-medium.en",
        label="Medium (English)", params="769M", download_mb=1530, vram_gb=5.0,
        speed="moderate", languages="English only",
        suitability="As Medium, better on English-only material.",
        tier="research",
    ),
    ModelSpec(
        key="distil-large-v3", repo="Systran/faster-distil-whisper-large-v3",
        label="Distil Large v3 (English)", params="756M", download_mb=1510, vram_gb=5.0,
        speed="very fast for its accuracy (~6x Large v3)", languages="English only",
        suitability=(
            "Near Large v3 accuracy on English at a fraction of the time. "
            "Excellent for English-only projects; cannot handle other "
            "languages at all."
        ),
        tier="research",
    ),
    ModelSpec(
        key="large-v3-turbo", repo="mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        label="Large v3 Turbo", params="809M", download_mb=1620, vram_gb=6.0,
        speed="fast (~4x Large v3)", languages="multilingual",
        suitability=(
            "The best general default: close to Large v3 accuracy, multilingual, "
            "and several times faster. Its pruned decoder is marginally weaker "
            "than Large v3 on heavily accented or overlapping speech."
        ),
        tier="research",
    ),
    ModelSpec(
        key="large-v3", repo="Systran/faster-whisper-large-v3",
        label="Large v3", params="1550M", download_mb=3090, vram_gb=10.0,
        speed="slowest (~1x realtime on GPU)", languages="multilingual",
        suitability=(
            "Highest accuracy available. Worth the time for poor recordings, "
            "strong accents, crosstalk, or anything destined for publication. "
            "Impractically slow on CPU for long files."
        ),
        tier="research",
    ),
    ModelSpec(
        key="large-v2", repo="Systran/faster-whisper-large-v2",
        label="Large v2", params="1550M", download_mb=3090, vram_gb=10.0,
        speed="slowest", languages="multilingual",
        suitability=(
            "The previous generation. Kept because it hallucinates less than "
            "v3 on some music-heavy or very noisy recordings."
        ),
        tier="research",
    ),

    # -- Apple Silicon (MLX / Metal) ---------------------------------------
    # These run on the GPU through MLX, Apple's unified-memory framework,
    # instead of CTranslate2 on CPU. Repo names and download sizes were
    # confirmed against the live mlx-community listing and its file sizes on
    # Hugging Face while this catalogue was written; that organisation's
    # listing moves independently of this one, so re-check before assuming
    # a newer quantization isn't already available - see savestate.md.
    ModelSpec(
        key="tiny-mlx", repo="mlx-community/whisper-tiny-mlx", engine="mlx",
        label="Tiny (Apple GPU)", params="39M", download_mb=74, vram_gb=0.0,
        speed="fastest, runs on the GPU via MLX", languages="multilingual",
        suitability=(
            "Smoke-testing the pipeline on Apple Silicon. Same accuracy "
            "ceiling as the CPU Tiny model - not for research transcripts."
        ),
        tier="draft",
    ),
    ModelSpec(
        key="tiny.en-mlx", repo="mlx-community/whisper-tiny.en-mlx", engine="mlx",
        label="Tiny English (Apple GPU)", params="39M", download_mb=74, vram_gb=0.0,
        speed="fastest, runs on the GPU via MLX", languages="English only",
        suitability="As Tiny (Apple GPU), slightly better on English. Draft quality only.",
        tier="draft",
    ),
    ModelSpec(
        key="base-mlx", repo="mlx-community/whisper-base-mlx", engine="mlx",
        label="Base (Apple GPU)", params="74M", download_mb=144, vram_gb=0.0,
        speed="very fast, runs on the GPU via MLX", languages="multilingual",
        suitability=(
            "Quick rough drafts on the GPU. Noticeably error-prone on proper "
            "nouns, overlapping speech and accents."
        ),
        tier="draft",
    ),
    ModelSpec(
        key="base.en-mlx", repo="mlx-community/whisper-base.en-mlx", engine="mlx",
        label="Base English (Apple GPU)", params="74M", download_mb=144, vram_gb=0.0,
        speed="very fast, runs on the GPU via MLX", languages="English only",
        suitability="As Base (Apple GPU), better on English. Draft quality.",
        tier="draft",
    ),
    ModelSpec(
        key="small-mlx", repo="mlx-community/whisper-small-mlx", engine="mlx",
        label="Small (Apple GPU)", params="244M", download_mb=481, vram_gb=0.0,
        speed="fast, runs on the GPU via MLX", languages="multilingual",
        suitability=(
            "A reasonable floor for clean audio, running on the GPU instead "
            "of CPU. Expect to correct names and technical terms by hand."
        ),
        tier="working",
    ),
    ModelSpec(
        key="small.en-mlx", repo="mlx-community/whisper-small.en-mlx", engine="mlx",
        label="Small English (Apple GPU)", params="244M", download_mb=481, vram_gb=0.0,
        speed="fast, runs on the GPU via MLX", languages="English only",
        suitability="As Small (Apple GPU), better on English-only material.",
        tier="working",
    ),
    ModelSpec(
        key="medium-mlx", repo="mlx-community/whisper-medium-mlx", engine="mlx",
        label="Medium (Apple GPU, fp16)", params="769M", download_mb=1525, vram_gb=0.0,
        speed="moderate, runs on the GPU via MLX", languages="multilingual",
        suitability=(
            "Good accuracy at full precision. A sound default for interview "
            "audio when there is unified memory to spare."
        ),
        tier="research",
    ),
    ModelSpec(
        key="medium-mlx-4bit", repo="mlx-community/whisper-medium-mlx-4bit", engine="mlx",
        label="Medium (Apple GPU, 4-bit)", params="769M", download_mb=512, vram_gb=0.0,
        speed="fast, runs on the GPU via MLX, smaller memory footprint",
        languages="multilingual",
        suitability=(
            "Medium's accuracy in a third of the memory and disk cost, for "
            "Macs with less unified memory to spare."
        ),
        tier="research",
    ),
    ModelSpec(
        key="medium.en-mlx", repo="mlx-community/whisper-medium.en-mlx", engine="mlx",
        label="Medium English (Apple GPU, fp16)", params="769M", download_mb=1525, vram_gb=0.0,
        speed="moderate, runs on the GPU via MLX", languages="English only",
        suitability="As Medium (Apple GPU), better on English-only material.",
        tier="research",
    ),
    ModelSpec(
        key="large-v3-turbo-mlx", repo="mlx-community/whisper-large-v3-turbo", engine="mlx",
        label="Large v3 Turbo (Apple GPU, fp16)", params="809M", download_mb=1614, vram_gb=0.0,
        speed="fast on the GPU via MLX (~4x Large v3)", languages="multilingual",
        suitability=(
            "The best general default on Apple Silicon: close to Large v3 "
            "accuracy, multilingual, and several times faster, running on "
            "the GPU through unified memory."
        ),
        tier="research",
    ),
    ModelSpec(
        key="large-v3-turbo-mlx-4bit", repo="mlx-community/whisper-large-v3-turbo-4bit", engine="mlx",
        label="Large v3 Turbo (Apple GPU, 4-bit)", params="809M", download_mb=464, vram_gb=0.0,
        speed="fast on the GPU via MLX, smaller memory footprint",
        languages="multilingual",
        suitability=(
            "Turbo's speed and near-Large-v3 accuracy at a third of the "
            "memory and disk cost - a good default on Macs with 16-24GB of "
            "unified memory."
        ),
        tier="research",
    ),
    ModelSpec(
        key="large-v3-mlx", repo="mlx-community/whisper-large-v3-mlx", engine="mlx",
        label="Large v3 (Apple GPU, fp16)", params="1550M", download_mb=3084, vram_gb=0.0,
        speed="fast on the GPU via MLX (Apple Silicon only)", languages="multilingual",
        suitability=(
            "Highest accuracy, running on the GPU through unified memory "
            "instead of CPU. The best choice on a Mac with 32GB+ of memory."
        ),
        tier="research",
    ),
    ModelSpec(
        key="large-v3-mlx-4bit", repo="mlx-community/whisper-large-v3-mlx-4bit", engine="mlx",
        label="Large v3 (Apple GPU, 4-bit)", params="1550M", download_mb=1947, vram_gb=0.0,
        speed="fast on the GPU via MLX, smaller memory footprint", languages="multilingual",
        suitability=(
            "Large v3 accuracy at roughly two thirds of the memory and disk "
            "cost of the full-precision version, for Macs with less unified "
            "memory to spare."
        ),
        tier="research",
    ),
)

CATALOG_BY_KEY = {m.key: m for m in CATALOG}


def model_spec(key: str) -> ModelSpec | None:
    return CATALOG_BY_KEY.get(key)


# ---------------------------------------------------------------------------
# Local model storage
# ---------------------------------------------------------------------------
# Models are stored under models/ as a HuggingFace cache so that
# huggingface_hub resolves them offline without any network attempt.


def model_cache_dir() -> Path:
    PATHS.models.mkdir(parents=True, exist_ok=True)
    return PATHS.models


def _repo_cache_name(repo: str) -> str:
    return "models--" + repo.replace("/", "--")


def local_model_path(key: str) -> Path | None:
    """Return the snapshot directory for a downloaded model, if present."""
    spec = model_spec(key)
    if spec is None:
        return None
    base = model_cache_dir() / _repo_cache_name(spec.repo) / "snapshots"
    if not base.is_dir():
        return None
    # CTranslate2 models are a single model.bin; MLX models are
    # *.safetensors/*.npz weights next to a config.json, no fixed filename.
    marker = "config.json" if spec.engine == "mlx" else "model.bin"
    for snapshot in sorted(base.iterdir(), reverse=True):
        if (snapshot / marker).exists():
            return snapshot
    return None


def is_downloaded(key: str) -> bool:
    return local_model_path(key) is not None


def model_disk_bytes(key: str) -> int:
    spec = model_spec(key)
    if spec is None:
        return 0
    base = model_cache_dir() / _repo_cache_name(spec.repo)
    if not base.is_dir():
        return 0
    total = 0
    for root, _dirs, files in os.walk(base):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def delete_model(key: str) -> bool:
    spec = model_spec(key)
    if spec is None:
        return False
    base = model_cache_dir() / _repo_cache_name(spec.repo)
    if not base.is_dir():
        return False
    shutil.rmtree(base, ignore_errors=True)
    return True


def downloaded_models() -> list[str]:
    return [m.key for m in CATALOG if is_downloaded(m.key)]


# ---------------------------------------------------------------------------
# Hardware probing
# ---------------------------------------------------------------------------


def _pascal_or_older(name: str) -> bool:
    """True for GPU families with no usable fp16 throughput.

    Pascal (GTX 10-series, Titan X/Xp, Quadro P) and older expose fp16 but at
    1/64th rate, so selecting float16 there is dramatically slower than int8.
    """
    lowered = name.lower()
    markers = (
        "gtx 10", "gtx 9", "gtx 7", "gtx titan", "titan x", "titan xp",
        "quadro p", "quadro m", "quadro k", "tesla p", "tesla m", "tesla k",
        "gt 10", "mx1", "mx2", "mx3",
    )
    return any(m in lowered for m in markers)


def probe_hardware() -> dict:
    """Detect CPU, GPU, and whether the CUDA runtime is actually usable.

    A GPU being physically present is not the same as being usable: the CUDA 12
    runtime that CTranslate2 4.x is built against requires an NVIDIA driver of
    roughly 525 or newer. Older drivers fail at runtime with a message no
    researcher should have to decode, so it is caught and explained here.
    """
    import ctranslate2

    info: dict = {
        "cpu_cores": os.cpu_count() or 1,
        "cpu_compute_types": [],
        "cuda_device_count": 0,
        "cuda_usable": False,
        "cuda_compute_types": [],
        "gpus": [],
        "driver_version": None,
        "apple_silicon": platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64"),
        "mlx_available": False,
        "unified_memory_gb": None,
        "notes": [],
        "warnings": [],
    }

    if info["apple_silicon"]:
        info["mlx_available"] = _mlx_available()
        info["unified_memory_gb"] = _apple_unified_memory_gb()

    try:
        info["cpu_compute_types"] = sorted(
            ctranslate2.get_supported_compute_types("cpu")
        )
    except Exception as exc:
        info["warnings"].append(f"Could not query CPU compute types: {exc}")

    try:
        info["cuda_device_count"] = int(ctranslate2.get_cuda_device_count())
    except Exception:
        info["cuda_device_count"] = 0

    gpu_names, driver = _query_nvidia_smi()
    info["gpus"] = gpu_names
    info["driver_version"] = driver

    if info["cuda_device_count"] > 0:
        try:
            info["cuda_compute_types"] = sorted(
                ctranslate2.get_supported_compute_types("cuda")
            )
            info["cuda_usable"] = True
        except Exception as exc:
            info["cuda_usable"] = False
            info["warnings"].append(_explain_cuda_failure(str(exc), driver))
    elif gpu_names:
        names = ", ".join(sorted(set(gpu_names)))
        count = len(gpu_names)
        old_driver = _driver_too_old(driver)
        if old_driver:
            info["warnings"].append(
                f"{count} NVIDIA GPU(s) detected ({names}), but driver "
                f"{driver} is too old for the CUDA 12 runtime the "
                "transcription engine is built against, which needs 527 or "
                "newer on Windows. The GPUs are therefore unavailable and "
                "transcription will run on CPU. Updating the NVIDIA driver is "
                "the single biggest speed improvement available on this "
                "machine; these cards are supported by current drivers."
            )
        else:
            info["warnings"].append(
                f"{count} NVIDIA GPU(s) detected ({names}) but the CUDA runtime "
                "cannot use them. This is usually a missing cuDNN 9 or cuBLAS "
                "12 library. Use 'Install GPU support' in AI Settings to add "
                "them. Transcription runs on CPU until then."
            )

    if info["cuda_usable"]:
        legacy = [n for n in gpu_names if _pascal_or_older(n)]
        if legacy:
            info["notes"].append(
                f"{legacy[0]} is a Pascal-generation or older GPU. It supports "
                "float16 but has no fast hardware path for it, so float16 runs "
                "slower than int8_float32 on this card. Leave compute type on "
                "'auto' unless you have a reason not to."
            )
    elif info["apple_silicon"]:
        if info["mlx_available"]:
            mem = info["unified_memory_gb"]
            info["notes"].append(
                (f"Apple Silicon detected with {mem} GB of unified memory, "
                 if mem else "Apple Silicon detected. Unified memory ")
                + "shared by the CPU and GPU with no separate VRAM ceiling to "
                "plan around. Transcription can run on the GPU via MLX - pick "
                "one of the '(Apple GPU)' models below, or leave Device on "
                "'auto'."
            )
        else:
            info["notes"].append(
                "Apple Silicon detected, but mlx-whisper is not installed, so "
                "transcription runs on CPU. Running "
                "'pip install -r requirements.txt' in the app's virtual "
                "environment should add GPU support."
            )
    else:
        info["notes"].append(
            f"Running on CPU with {info['cpu_cores']} logical cores. Expect "
            "roughly realtime speed with Medium and several times slower than "
            "realtime with Large v3."
        )

    return info


def _mlx_available() -> bool:
    """Cheap check for whether mlx-whisper is installed, without importing it."""
    return (
        importlib.util.find_spec("mlx") is not None
        and importlib.util.find_spec("mlx_whisper") is not None
    )


def _apple_unified_memory_gb() -> float | None:
    """Total system memory on Apple Silicon.

    This doubles as the GPU's memory ceiling, since there is no separate
    VRAM to query - unlike the NVIDIA case above, this is simple and
    reliable, so it is safe to state as a number rather than hedge it.
    """
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    try:
        return round(int(out.stdout.strip()) / 1e9, 1)
    except ValueError:
        return None


def _driver_too_old(driver: str | None, minimum: float = 527.0) -> bool:
    """True when the NVIDIA driver predates CUDA 12 support."""
    if not driver:
        return False
    try:
        parts = driver.split(".")
        return float(f"{parts[0]}.{parts[1] if len(parts) > 1 else 0}") < minimum
    except (ValueError, IndexError):
        return False


def _query_nvidia_smi() -> tuple[list[str], str | None]:
    """Ask nvidia-smi for GPU names and driver version. Never raises."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return [], None
    import subprocess

    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return [], None
    if out.returncode != 0:
        return [], None
    names: list[str] = []
    driver: str | None = None
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if parts and parts[0]:
            names.append(parts[0])
        if len(parts) > 1 and parts[1]:
            driver = parts[1]
    return names, driver


def _explain_cuda_failure(message: str, driver: str | None) -> str:
    lowered = message.lower()
    if "driver version is insufficient" in lowered:
        extra = f" The installed driver is {driver}." if driver else ""
        return (
            "An NVIDIA GPU is present but the installed driver is too old for "
            "the CUDA 12 runtime that the transcription engine is built "
            f"against.{extra} Updating the NVIDIA driver to 527 or newer "
            "unlocks GPU transcription, which is several times faster. Until "
            "then transcription runs on CPU, which works correctly but is "
            "slower."
        )
    if "cudnn" in lowered or "cublas" in lowered:
        return (
            "An NVIDIA GPU is present but the CUDA support libraries are "
            "missing. Use 'Install GPU support' in AI Settings to add cuDNN 9 "
            f"and cuBLAS 12. Original error: {message}"
        )
    return (
        "An NVIDIA GPU is present but could not be initialised, so "
        f"transcription will run on CPU. Original error: {message}"
    )


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


def recommend(hardware: dict | None = None) -> dict:
    """Recommend a model and compute type, with the reasoning stated."""
    hw = hardware or probe_hardware()
    cores = hw.get("cpu_cores") or 1

    if hw.get("apple_silicon") and hw.get("mlx_available"):
        mem = hw.get("unified_memory_gb") or 0
        if mem >= 32:
            model, reason_mem = "large-v3-mlx", (
                f"With {mem:.0f} GB of unified memory, this Mac can "
                "comfortably run Large v3 on the GPU at full precision."
            )
        elif mem >= 20:
            model, reason_mem = "large-v3-turbo-mlx", (
                f"With {mem:.0f} GB of unified memory, Large v3 Turbo gives "
                "close to Large v3 accuracy at roughly four times the speed, "
                "which makes it the right default for interview-length audio."
            )
        elif mem >= 12:
            model, reason_mem = "large-v3-turbo-mlx-4bit", (
                f"With {mem:.0f} GB of unified memory, the 4-bit Turbo keeps "
                "Turbo's speed and near-Large-v3 accuracy while leaving "
                "enough memory free for everything else running on the Mac."
            )
        else:
            model, reason_mem = "small-mlx", (
                f"With {mem:.0f} GB of unified memory, Small is the largest "
                "model that leaves headroom for the rest of the system."
            )
        return {
            "model": model,
            "compute_type": "mlx-native",
            "device": "mlx",
            "reason": (
                reason_mem + " Running on the GPU via MLX is several times "
                "faster than the CPU path, and uses the same unified memory "
                "the rest of macOS does, so there is no separate VRAM limit "
                "to plan around."
            ),
            "alternatives": [
                {
                    "model": "large-v3-mlx",
                    "when": "Publication-quality passes, if unified memory allows.",
                },
                {
                    "model": "medium-mlx-4bit",
                    "when": "Faster turnaround with less memory pressure than Turbo.",
                },
                {
                    "model": "small-mlx",
                    "when": "Fast rough drafts.",
                },
            ],
        }

    if hw.get("cuda_usable"):
        gpu = (hw.get("gpus") or ["GPU"])[0]
        legacy = _pascal_or_older(gpu)
        compute = "int8_float32" if legacy else "float16"
        return {
            "model": "large-v3-turbo",
            "compute_type": compute,
            "device": "cuda",
            "reason": (
                f"{gpu} can run the engine on GPU. Large v3 Turbo gives close "
                "to Large v3 accuracy at roughly four times the speed, which "
                "makes it the right default for interview-length audio. "
                + (
                    f"Because {gpu} is Pascal-generation, {compute} is chosen "
                    "over float16, which that architecture runs slowly."
                    if legacy else
                    "float16 is the fastest precision on this card with no "
                    "meaningful accuracy cost."
                )
            ),
            "alternatives": [
                {
                    "model": "large-v3",
                    "when": "Publication-quality passes, heavy accents, "
                            "crosstalk, or poor recording conditions.",
                },
                {
                    "model": "distil-large-v3",
                    "when": "English-only projects where speed matters most.",
                },
            ],
        }

    # CPU path: recommend by core count, and be explicit about the time cost.
    if cores >= 12:
        model, reason_core = "medium", (
            f"With {cores} logical cores, Medium runs at roughly realtime on "
            "CPU, meaning a one-hour interview takes about an hour."
        )
    elif cores >= 6:
        model, reason_core = "small", (
            f"With {cores} logical cores, Small is the largest model that "
            "finishes in a sensible time on CPU."
        )
    else:
        model, reason_core = "base", (
            f"With only {cores} logical cores, anything larger than Base will "
            "be painfully slow. Treat the output as a draft to correct."
        )

    return {
        "model": model,
        "compute_type": "int8",
        "device": "cpu",
        "reason": (
            reason_core
            + " int8 is chosen because it is two to three times faster than "
            "float32 on CPU with a negligible accuracy difference."
        ),
        "alternatives": [
            {
                "model": "distil-large-v3",
                "when": "English-only audio where you want better accuracy "
                        "than Medium without Large v3 runtimes.",
            },
            {
                "model": "large-v3-turbo",
                "when": "Accuracy matters more than turnaround and you can "
                        "leave the transcription running unattended.",
            },
        ],
    }


def catalog_payload(hardware: dict | None = None) -> dict:
    hw = hardware or probe_hardware()
    rec = recommend(hw)
    models = []
    for spec in CATALOG:
        entry = asdict(spec)
        entry["downloaded"] = is_downloaded(spec.key)
        entry["disk_bytes"] = model_disk_bytes(spec.key) if entry["downloaded"] else 0
        entry["recommended"] = spec.key == rec["model"]
        entry["fits_vram"] = True
        if hw.get("cuda_usable") and hw.get("gpus"):
            entry["fits_vram"] = True  # VRAM query is unreliable; do not bluff.
        models.append(entry)
    return {"models": models, "recommendation": rec, "hardware": hw}
