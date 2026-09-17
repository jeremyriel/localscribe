"""Audio and video handling, built entirely on PyAV.

PyAV ships its own statically linked FFmpeg libraries, so Local Scribe can
probe, decode and transcode audio without an `ffmpeg` binary on PATH. That
matters for a research workstation where installing system tooling may not be
permitted.

Three jobs live here:

1. ``probe`` -- read container/stream facts and reject unusable files early
   with a message that says what is actually wrong.
2. ``decode_to_pcm`` -- produce the 16 kHz mono float32 stream Whisper wants,
   streamed in blocks so a three-hour interview does not sit in memory twice.
3. ``make_preview`` / ``waveform_peaks`` -- browser-playable audio and a
   downsampled envelope for the editor's time ribbon.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict
from fractions import Fraction
from pathlib import Path

import numpy as np

TARGET_RATE = 16_000  # Whisper is trained at 16 kHz mono
PREVIEW_RATE = 22_050
PREVIEW_BITRATE = 64_000

# Extensions offered in the file picker. Validation is by actual probe result,
# not by extension, so a mislabelled file still works.
AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
    ".wma", ".aif", ".aiff", ".aifc", ".amr", ".ac3", ".mka", ".caf", ".au",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".wmv", ".flv",
    ".mpg", ".mpeg", ".mts", ".m2ts", ".ts", ".3gp", ".3g2", ".ogv", ".asf",
    ".vob", ".divx", ".f4v", ".rm", ".rmvb",
}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

# Containers/codecs a browser <audio> element can be trusted to play directly.
BROWSER_SAFE_AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".flac"}
BROWSER_SAFE_VIDEO_EXT = {".mp4", ".m4v", ".webm", ".mov"}
BROWSER_SAFE_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le"}

# PyAV reports the *decoder* name, which is not always the codec name: mp3 files
# come back as "mp3float" and some builds report "aac_fixed" or "vorbis" via
# "libvorbis". Normalising avoids wrongly transcoding a file a browser can
# already play.
_CODEC_ALIASES = {
    "mp3float": "mp3",
    "mp3adu": "mp3",
    "mp3adufloat": "mp3",
    "mp3on4": "mp3",
    "mp3on4float": "mp3",
    "aac_fixed": "aac",
    "aac_latm": "aac",
    "libvorbis": "vorbis",
    "libopus": "opus",
    "opus_fixed": "opus",
    "pcm_f32le": "pcm_s16le",
    "pcm_s24le": "pcm_s16le",
    "pcm_s32le": "pcm_s16le",
}


def normalise_codec(name):
    """Map a decoder name onto the codec name a browser would recognise."""
    if not name:
        return None
    lowered = str(name).lower()
    if lowered in _CODEC_ALIASES:
        return _CODEC_ALIASES[lowered]
    for suffix in ("float", "_fixed", "_at", "_mf"):
        if lowered.endswith(suffix):
            trimmed = lowered[: -len(suffix)]
            if trimmed in BROWSER_SAFE_AUDIO_CODECS:
                return trimmed
    return lowered


class MediaError(Exception):
    """A file cannot be used, with a reason fit to show a researcher."""


@dataclass
class MediaInfo:
    path: str
    filename: str
    size_bytes: int
    container: str
    duration: float | None
    has_audio: bool
    has_video: bool
    audio_codec: str | None
    sample_rate: int | None
    channels: int | None
    video_codec: str | None
    width: int | None
    height: int | None
    browser_playable: bool

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def kind(self) -> str:
        return "video" if self.has_video else "audio"


def is_supported_extension(name: str) -> bool:
    return Path(name).suffix.lower() in MEDIA_EXTENSIONS


def probe(path: str | Path, display_name: str | None = None) -> MediaInfo:
    """Inspect a media file. Raises MediaError with a usable message.

    ``display_name`` is the name to use in messages. Stored media carries an
    internal id prefix, which must never appear in something a researcher reads.
    """
    import av

    p = Path(path)
    shown = display_name or p.name
    if not p.exists():
        raise MediaError(f"File not found: {shown}")
    if p.stat().st_size == 0:
        raise MediaError(f"{shown} is empty (0 bytes).")

    try:
        container = av.open(str(p))
    except av.AVError as exc:
        raise MediaError(
            f"{shown} could not be opened as media. It may be corrupt, "
            f"partially copied, or not an audio/video file at all. ({exc})"
        ) from exc

    with container:
        astreams = [s for s in container.streams if s.type == "audio"]
        vstreams = [s for s in container.streams if s.type == "video"]

        duration = None
        if container.duration is not None:
            duration = float(container.duration) / 1_000_000.0
        if (duration is None or duration <= 0) and astreams:
            st = astreams[0]
            if st.duration is not None and st.time_base:
                duration = float(st.duration * st.time_base)

        audio = astreams[0] if astreams else None
        video = vstreams[0] if vstreams else None

        info = MediaInfo(
            path=str(p),
            filename=shown,
            size_bytes=p.stat().st_size,
            container=getattr(container.format, "name", "unknown"),
            duration=duration,
            has_audio=bool(astreams),
            has_video=bool(vstreams),
            audio_codec=(normalise_codec(audio.codec_context.name) if audio else None),
            sample_rate=(audio.codec_context.sample_rate if audio else None),
            channels=(getattr(audio.codec_context, "channels", None) if audio else None),
            video_codec=(video.codec_context.name if video else None),
            width=(video.codec_context.width if video else None),
            height=(video.codec_context.height if video else None),
            browser_playable=False,
        )

    if not info.has_audio:
        if info.has_video:
            raise MediaError(
                f"{shown} is a video with no audio track, so there is nothing "
                "to transcribe. Check that the recording captured audio."
            )
        raise MediaError(f"{shown} contains no audio stream.")

    info.browser_playable = _is_browser_playable(p, info)
    return info


def _is_browser_playable(p: Path, info: MediaInfo) -> bool:
    ext = p.suffix.lower()
    if info.audio_codec not in BROWSER_SAFE_AUDIO_CODECS:
        return False
    if info.has_video:
        return ext in BROWSER_SAFE_VIDEO_EXT
    return ext in BROWSER_SAFE_AUDIO_EXT


def iter_pcm_blocks(path: str | Path, block_seconds: float = 30.0):
    """Yield float32 mono blocks at 16 kHz.

    Resampling happens inside FFmpeg via AudioResampler, so this handles any
    input rate, channel layout and sample format, including the 48 kHz stereo
    AAC that most phone and camera video carries.
    """
    import av

    p = Path(path)
    target_block = int(TARGET_RATE * block_seconds)

    with av.open(str(p)) as container:
        astreams = [s for s in container.streams if s.type == "audio"]
        if not astreams:
            raise MediaError(f"{p.name} contains no audio stream.")
        stream = astreams[0]
        stream.thread_type = "AUTO"

        resampler = av.AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)
        buf = np.empty(0, dtype=np.int16)

        def append(resampled, buf):
            samples = resampled.to_ndarray().reshape(-1)
            return np.concatenate([buf, samples]) if buf.size else samples

        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                buf = append(resampled, buf)
                while buf.size >= target_block:
                    yield buf[:target_block]
                    buf = buf[target_block:]

        # Flush the resampler's internal buffer.
        for resampled in resampler.resample(None):
            buf = append(resampled, buf)
        while buf.size >= target_block:
            yield buf[:target_block]
            buf = buf[target_block:]
        if buf.size:
            yield buf


def decode_to_pcm(path: str | Path, progress=None) -> np.ndarray:
    """Decode a whole file to a float32 mono 16 kHz array in [-1, 1]."""
    chunks: list[np.ndarray] = []
    total = 0
    for block in iter_pcm_blocks(path):
        chunks.append(block)
        total += block.size
        if progress is not None:
            progress(total / TARGET_RATE)
    if not chunks:
        raise MediaError(f"No audio could be decoded from {Path(path).name}.")
    pcm = np.concatenate(chunks).astype(np.float32) / 32768.0
    return pcm


def measure_duration(path: str | Path) -> float:
    """Exact duration by decoding. Used when container metadata is missing."""
    total = 0
    for block in iter_pcm_blocks(path):
        total += block.size
    return total / TARGET_RATE


def make_preview(src: str | Path, dest: str | Path) -> Path:
    """Write a compact browser-playable .m4a companion for the editor.

    Formats such as .mkv, .avi and .wmv will not play in a browser at all, and
    a 2 GB source video is wasteful to stream just to hear speech. The original
    file is never modified.
    """
    import av

    src_p, dest_p = Path(src), Path(dest)
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_p.with_suffix(dest_p.suffix + ".part")

    with av.open(str(src_p)) as inp:
        astreams = [s for s in inp.streams if s.type == "audio"]
        if not astreams:
            raise MediaError(f"{src_p.name} contains no audio stream.")
        in_stream = astreams[0]
        in_stream.thread_type = "AUTO"

        with av.open(str(tmp), mode="w", format="mp4") as out:
            out_stream = out.add_stream("aac", rate=PREVIEW_RATE)
            out_stream.bit_rate = PREVIEW_BITRATE
            try:
                out_stream.layout = "mono"
            except Exception:
                pass
            out_stream.time_base = Fraction(1, PREVIEW_RATE)

            resampler = av.AudioResampler(
                format=out_stream.format.name,
                layout="mono",
                rate=PREVIEW_RATE,
            )
            for frame in inp.decode(in_stream):
                for resampled in resampler.resample(frame):
                    resampled.pts = None
                    for packet in out_stream.encode(resampled):
                        out.mux(packet)
            for resampled in resampler.resample(None):
                resampled.pts = None
                for packet in out_stream.encode(resampled):
                    out.mux(packet)
            for packet in out_stream.encode(None):
                out.mux(packet)

    tmp.replace(dest_p)
    return dest_p


def waveform_peaks(path: str | Path, buckets: int = 2400, duration: float | None = None) -> dict:
    """Downsampled min/max envelope for the editor's time ribbon.

    Returns normalised magnitudes in 0..1 rather than raw samples, so the
    canvas code does no scaling and the cached JSON stays small.
    """
    if duration is None or duration <= 0:
        duration = measure_duration(path)
    buckets = max(200, min(20_000, int(buckets)))
    samples_per_bucket = max(1, int((duration * TARGET_RATE) / buckets))

    peaks: list[float] = []
    carry = np.empty(0, dtype=np.int16)

    for block in iter_pcm_blocks(path):
        data = np.concatenate([carry, block]) if carry.size else block
        usable = (data.size // samples_per_bucket) * samples_per_bucket
        if usable:
            frames = data[:usable].reshape(-1, samples_per_bucket).astype(np.float32)
            peaks.extend((np.abs(frames).max(axis=1) / 32768.0).tolist())
        carry = data[usable:]

    if carry.size:
        peaks.append(float(np.abs(carry.astype(np.float32)).max() / 32768.0))

    ceiling = max(peaks) if peaks else 0.0
    if ceiling > 0:
        # Normalise so quiet recordings still show a readable waveform.
        scale = 1.0 / ceiling
        peaks = [min(1.0, p * scale) for p in peaks]

    return {
        "duration": round(float(duration), 3),
        "buckets": len(peaks),
        "peaks": [round(p, 4) for p in peaks],
    }


def write_waveform_cache(path: str | Path, dest: str | Path, duration: float | None = None) -> Path:
    data = waveform_peaks(path, duration=duration)
    dest_p = Path(dest)
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    dest_p.write_text(json.dumps(data), encoding="utf-8")
    return dest_p


def describe(info: MediaInfo) -> str:
    """One-line human summary for the console."""
    bits = [info.container]
    if info.has_video:
        res = f"{info.width}x{info.height}" if info.width else "video"
        bits.append(f"{info.video_codec} {res}")
    ch = {1: "mono", 2: "stereo"}.get(info.channels or 0, f"{info.channels}ch")
    bits.append(f"{info.audio_codec} {info.sample_rate or '?'}Hz {ch}")
    if info.duration:
        mins = math.floor(info.duration / 60)
        secs = int(info.duration % 60)
        bits.append(f"{mins}m{secs:02d}s")
    return ", ".join(str(b) for b in bits)
