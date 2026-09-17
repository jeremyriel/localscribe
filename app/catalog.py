"""Whisper model catalogue, hardware probing, and local model management.

The catalogue is the honest part of the settings page. Each entry carries what
a researcher actually needs to choose: how large the download is, roughly how
fast it runs, and what it is and is not suitable for. Recommendations are
computed from detected hardware and always state their reason, because a
recommendation without a reason is just a default.
"""

from __future__ import annotations

import os
import shutil
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
    for snapshot in sorted(base.iterdir(), reverse=True):
        if (snapshot / "model.bin").exists():
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
        "notes": [],
        "warnings": [],
    }

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
    else:
        info["notes"].append(
            f"Running on CPU with {info['cpu_cores']} logical cores. Expect "
            "roughly realtime speed with Medium and several times slower than "
            "realtime with Large v3."
        )

    return info


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
