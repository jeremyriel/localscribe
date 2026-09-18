"""Paths, the fixed port, and the persisted settings document for Local Scribe.

Everything the app stores lives inside the application directory so that two
different local Python services can never contend for the same lock file or
settings blob.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_NAME = "Local Scribe"
APP_SLUG = "localscribe"

# Identity signature used by the launcher to tell "a stale Local Scribe" apart
# from "somebody else's web service" before it terminates anything.
APP_SIGNATURE = "localscribe/instance/v1"

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
# 43707 is deliberate, not arbitrary:
#   * It sits below the Windows dynamic/ephemeral floor (49152), so the OS will
#     never hand this port to an outbound socket and cause an intermittent bind
#     failure -- the hazard with fixed listeners in the 49152-65535 range.
#   * It is unassigned by IANA and clear of real services in the 40000-49151
#     band (WinRM 47001, BACnet 47808, EtherNet/IP 44818, Crestron 41794).
#   * It avoids every port the operator reported in use by other projects.
# There is intentionally NO port-walking fallback: relocating to a neighbouring
# port is precisely how local services end up talking to each other's clients.
DEFAULT_PORT = 43707
HOST = "127.0.0.1"  # loopback only; never 0.0.0.0


def resolve_port() -> int:
    """The port to bind. Overridable only by explicit environment variable."""
    raw = os.environ.get("LOCALSCRIBE_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PORT
    if not 1024 <= port <= 65535:
        return DEFAULT_PORT
    return port


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def _default_data_root() -> Path:
    """Where user data (projects, models, logs, settings) lives.

    Defaults to ROOT, matching run.command/.sh/.bat, where the whole app is
    one folder the user already owns. The packaged desktop installers (see
    packaging/) set LOCALSCRIBE_DATA_DIR instead, to a proper per-user data
    directory (e.g. ~/Library/Application Support/Local Scribe on macOS):
    an installed .app/Program Files folder is not reliably writable, gets
    wiped on every update or reinstall, and - once the bundle is
    code-signed - writing inside it breaks the signature seal. Research
    data has to survive all of that.
    """
    override = os.environ.get("LOCALSCRIBE_DATA_DIR")
    return Path(override).expanduser().resolve() if override else ROOT


DATA_ROOT = _default_data_root()


@dataclass(frozen=True)
class AppPaths:
    root: Path = ROOT
    app: Path = ROOT / "app"
    static: Path = ROOT / "app" / "static"
    templates: Path = ROOT / "app" / "templates"
    assets: Path = ROOT / "assets"
    models: Path = DATA_ROOT / "models"
    projects: Path = DATA_ROOT / "projects"
    logs: Path = DATA_ROOT / "logs"
    settings_file: Path = DATA_ROOT / "settings.json"
    pidfile: Path = DATA_ROOT / "logs" / "localscribe.pid"
    version_file: Path = ROOT / "VERSION"

    def ensure(self) -> None:
        for p in (self.models, self.projects, self.logs):
            p.mkdir(parents=True, exist_ok=True)


PATHS = AppPaths()


def app_version() -> str:
    try:
        return PATHS.version_file.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


# ---------------------------------------------------------------------------
# Settings schema
# ---------------------------------------------------------------------------
# Declared as data so the settings page, validation, and "restore defaults" all
# derive from one source. Each field carries the metadata the UI needs to render
# an honest control with an explanation of what it actually does.

FieldSpec = dict


def _f(key, label, kind, default, help="", **extra):
    spec = {
        "key": key,
        "label": label,
        "kind": kind,
        "default": default,
        "help": help,
    }
    spec.update(extra)
    return spec


SETTINGS_SCHEMA = [
    {
        "id": "engine",
        "title": "Engine",
        "blurb": (
            "Which Whisper model runs, and on what hardware. Changing any of "
            "these reloads the model on the next transcription."
        ),
        "fields": [
            _f("model", "Model", "model_select", "large-v3-turbo",
               "Pick from the catalogue below. Larger models are more accurate "
               "and slower; the recommendation panel accounts for your hardware."),
            _f("device", "Device", "device_select", "auto",
               "'auto' prefers CUDA when a usable GPU and cuDNN are present, "
               "otherwise CPU. Pick a specific GPU index when you have several."),
            _f("compute_type", "Compute type", "select", "auto",
               "Numeric precision. 'auto' chooses a safe precision for the "
               "detected card. Note that float16 is a trap on Pascal GPUs "
               "(GTX 10-series): it is supported but has no fast hardware path, "
               "so it runs slower than int8_float32.",
               options=["auto", "int8", "int8_float32", "int8_float16",
                        "float16", "bfloat16", "float32"]),
            _f("cpu_threads", "CPU threads", "slider", 0,
               "Threads used for CPU inference. 0 lets CTranslate2 decide "
               "(usually physical core count). Ignored on GPU.",
               min=0, max=64, step=1),
            _f("num_workers", "Model workers", "slider", 1,
               "Parallel model replicas. Keep at 1 unless you have VRAM to "
               "spare; extra workers multiply memory use.",
               min=1, max=4, step=1),
        ],
    },
    {
        "id": "decoding",
        "title": "Decoding",
        "blurb": (
            "How the decoder searches for the transcript. Wider searches are "
            "more accurate on difficult audio and cost proportionally more time."
        ),
        "fields": [
            _f("beam_size", "Beam size", "slider", 5,
               "Number of candidate transcripts explored in parallel. 1 is "
               "greedy and fastest; 5 is the Whisper default; above 8 the "
               "accuracy gain is usually negligible for the added time.",
               min=1, max=10, step=1),
            _f("best_of", "Best of (sampling)", "slider", 5,
               "Candidates sampled when temperature is above zero, i.e. only "
               "during the fallback ladder below.",
               min=1, max=10, step=1),
            _f("patience", "Beam patience", "number", 1.0,
               "Beam-search patience factor. 1.0 is standard; higher values let "
               "beams run longer before pruning.",
               min=0.5, max=4.0, step=0.1),
            _f("temperature_fallback", "Temperature fallback ladder", "text",
               "0.0, 0.2, 0.4, 0.6, 0.8, 1.0",
               "When a segment fails the quality gates, Whisper retries it at "
               "each temperature in turn. A single value such as '0.0' disables "
               "the retry behaviour entirely."),
            _f("length_penalty", "Length penalty", "number", 1.0,
               "Above 1.0 favours longer transcripts, below 1.0 favours shorter.",
               min=0.1, max=3.0, step=0.1),
            _f("repetition_penalty", "Repetition penalty", "number", 1.0,
               "Above 1.0 discourages the decoder from repeating itself. Raise "
               "slightly (1.05-1.2) if you see looping text.",
               min=1.0, max=2.0, step=0.01),
            _f("no_repeat_ngram_size", "No-repeat n-gram size", "slider", 0,
               "Forbids repeating any n-gram of this length. 0 disables. A "
               "blunt but effective instrument against stuck loops.",
               min=0, max=10, step=1),
        ],
    },
    {
        "id": "tokens",
        "title": "Token budget and chunking",
        "blurb": (
            "Whisper's decoder has a hard 448-token context window per "
            "30-second audio window. These controls work inside that limit "
            "rather than extending it."
        ),
        "fields": [
            _f("max_new_tokens", "Max new tokens per window", "slider", 0,
               "Caps tokens generated for each 30-second window. 0 means no "
               "extra cap, so Whisper's own 448-token limit applies. Lowering "
               "this can cut off dense speech mid-sentence, so treat it as a "
               "safety valve against runaway generation rather than a tuning "
               "knob.",
               min=0, max=448, step=8),
            _f("chunk_length", "Chunk length (seconds)", "slider", 30,
               "Audio window fed to the encoder. Whisper was trained at 30s and "
               "that is strongly recommended; other values are experimental.",
               min=10, max=30, step=5),
            _f("prompt_reset_on_temperature", "Prompt reset temperature", "number", 0.5,
               "When the fallback ladder climbs above this temperature, the "
               "carried-over prompt is discarded to break error cascades.",
               min=0.0, max=1.0, step=0.1),
        ],
    },
    {
        "id": "quality",
        "title": "Quality gates",
        "blurb": (
            "Thresholds that decide when a decoded segment is untrustworthy and "
            "should be retried or dropped. These are the main defence against "
            "hallucinated text over silence."
        ),
        "fields": [
            _f("condition_on_previous_text", "Condition on previous text", "bool", True,
               "Feeds prior text as context, which improves coherence and "
               "consistent spelling of names. It can also propagate an error "
               "into a repetition loop; switch it off if a transcript starts "
               "repeating a phrase indefinitely."),
            _f("compression_ratio_threshold", "Compression ratio threshold", "number", 2.4,
               "A segment whose gzip compression ratio exceeds this is judged "
               "repetitive and retried. Lower is stricter.",
               min=1.0, max=10.0, step=0.1),
            _f("log_prob_threshold", "Average log-probability threshold", "number", -1.0,
               "Segments whose mean token log-probability falls below this are "
               "treated as failed. Closer to zero is stricter.",
               min=-5.0, max=0.0, step=0.1),
            _f("no_speech_threshold", "No-speech threshold", "number", 0.6,
               "Above this no-speech probability, and with a failing log-prob, "
               "the window is considered silent and skipped.",
               min=0.0, max=1.0, step=0.05),
            _f("hallucination_silence_threshold", "Hallucination silence threshold (s)", "number", 0.0,
               "With word timestamps on, skip silent stretches longer than this "
               "when a hallucination is suspected. 0 disables.",
               min=0.0, max=10.0, step=0.5),
        ],
    },
    {
        "id": "vad",
        "title": "Voice activity detection",
        "blurb": (
            "Silero VAD trims silence before transcription. On long interviews "
            "with pauses this both speeds things up and suppresses hallucinated "
            "text over silence. It runs locally through onnxruntime."
        ),
        "fields": [
            _f("vad_filter", "Enable VAD filter", "bool", True,
               "Strongly recommended for interview and focus-group recordings."),
            _f("vad_threshold", "Speech probability threshold", "number", 0.5,
               "Higher values require more confident speech, trimming more "
               "audio but risking clipped soft speech.",
               min=0.1, max=0.9, step=0.05),
            _f("vad_min_speech_duration_ms", "Min speech duration (ms)", "slider", 250,
               "Speech runs shorter than this are discarded as noise.",
               min=0, max=2000, step=50),
            _f("vad_min_silence_duration_ms", "Min silence duration (ms)", "slider", 2000,
               "Silence must last this long before it is cut. Shorter values "
               "cut more aggressively and can clip natural pauses.",
               min=100, max=5000, step=100),
            _f("vad_speech_pad_ms", "Speech padding (ms)", "slider", 400,
               "Padding kept either side of detected speech, so word onsets and "
               "trailing consonants are not clipped.",
               min=0, max=1000, step=50),
        ],
    },
    {
        "id": "language",
        "title": "Language and vocabulary",
        "blurb": (
            "Forcing the language avoids misdetection on short or accented "
            "openings. Domain vocabulary measurably improves proper nouns."
        ),
        "fields": [
            _f("language", "Language", "text", "auto",
               "ISO code such as 'en', 'es' or 'zh', or 'auto' to detect from "
               "the first 30 seconds. Forcing the correct code is more reliable "
               "than detection on recordings that open with noise or music."),
            _f("task", "Task", "select", "transcribe",
               "'transcribe' keeps the spoken language. 'translate' renders "
               "English output from non-English speech.",
               options=["transcribe", "translate"]),
            _f("initial_prompt", "Initial prompt", "textarea", "",
               "Free text given to the decoder as prior context. Listing names, "
               "jargon and acronyms here biases spelling toward them. Projects "
               "can override this with their own glossary."),
            _f("hotwords", "Hotwords", "textarea", "",
               "Alternative to the initial prompt for boosting specific terms. "
               "Leave empty unless you need it; using both at once is redundant."),
            _f("multilingual", "Allow language switching mid-file", "bool", False,
               "Re-detects language per window. Useful for genuinely bilingual "
               "recordings, noisier otherwise."),
        ],
    },
    {
        "id": "output",
        "title": "Output and captions",
        "blurb": "How transcripts and caption files are written.",
        "fields": [
            _f("formats", "Export formats", "multiselect",
               ["txt", "md", "docx", "vtt", "srt", "json"],
               "Written into each document's outputs/ folder after "
               "transcription and after every re-timestamp.",
               options=["txt", "md", "docx", "vtt", "srt", "json"]),
            _f("vtt_max_chars", "Caption max characters per line", "slider", 42,
               "Captions are split at word boundaries, never mid-word. 32 to 42 "
               "is the broadcast convention.",
               min=20, max=90, step=1),
            _f("vtt_max_lines", "Caption max lines per cue", "slider", 2,
               "Two lines is the accessibility convention.",
               min=1, max=3, step=1),
            _f("vtt_max_duration", "Caption max duration (s)", "number", 6.0,
               "Long cues are split so captions stay readable.",
               min=1.0, max=15.0, step=0.5),
            _f("txt_include_timestamps", "Include timestamps in .txt", "bool", False,
               "Off gives clean prose for reading and coding; on gives a "
               "timestamped log."),
            _f("txt_include_speakers", "Include speaker labels", "bool", True,
               "Uses the speaker names you assign in the editor."),
            _f("paragraph_gap", "Paragraph break on pause (s)", "number", 1.5,
               "Silence longer than this starts a new paragraph. It does not "
               "start a new speaking turn, so a pause never causes the "
               "speaker's name to be printed again.",
               min=0.0, max=10.0, step=0.25),
            _f("show_pause_markers", "Mark long pauses in the text", "bool", True,
               "Writes a marker such as (4.2s pause) where the recording goes "
               "quiet mid-turn. Useful when a silence is itself meaningful."),
            _f("pause_marker_seconds", "Minimum pause to mark (s)", "number", 2.0,
               "Only pauses at least this long get a marker, so ordinary "
               "breath pauses do not clutter the transcript.",
               min=0.5, max=30.0, step=0.5),
            _f("speaker_label_style", "Speaker label", "select", "name",
               "'name' prints the full name or pseudonym. 'short' prints the "
               "acronym instead, which suits captions and narrow columns.",
               options=["name", "short"]),
            _f("caption_speaker_style", "Caption speaker label", "select", "short",
               "Captions have very little room, so the acronym is usually the "
               "better choice for .vtt and .srt files.",
               options=["name", "short", "none"]),
        ],
    },
    {
        "id": "proofread",
        "title": "Proofreading",
        "blurb": (
            "Marks probable errors in the transcript editor: red for words not "
            "in the dictionary, blue for likely transcription artefacts. "
            "Nothing is ever changed automatically, and nothing is written into "
            "your exported files."
        ),
        "fields": [
            _f("proofread_spelling", "Check spelling", "bool", True,
               "Underlines words that are not in the dictionary, your project "
               "glossary, or the words you have accepted for this document. "
               "American and British spellings are both accepted."),
            _f("proofread_artefacts", "Check for transcription artefacts", "bool", True,
               "Underlines doubled words, repeated-phrase loops, a/an "
               "mismatches, spacing slips and lost sentence capitals. It does "
               "NOT check grammar: people speak in fragments and false starts, "
               "and flagging that would bury real errors and tempt you into "
               "editing the data."),
            _f("proofread_suggestions", "Suggestions to offer", "slider", 5,
               "How many corrections to list when you right-click a flagged "
               "word. 0 disables suggestions and only marks the word.",
               min=0, max=10, step=1),
        ],
    },
    {
        "id": "privacy",
        "title": "Privacy and network",
        "blurb": (
            "Local Scribe makes no outbound request except a deliberate model "
            "download. Offline Lock enforces that at the process level."
        ),
        "fields": [
            _f("offline_lock", "Offline lock", "bool", False,
               "When on, every non-loopback network call is blocked inside this "
               "process, including model downloads. Turn it on once your models "
               "are downloaded. It is enforced by refusing outbound sockets, "
               "not merely by convention."),
            _f("keep_preview_audio", "Keep browser preview audio", "bool", True,
               "Formats such as .mkv and .avi cannot play in a browser, so a "
               "compact .m4a companion is created for the editor. Turning this "
               "off saves disk but disables playback for those formats."),
            _f("autosave_revisions", "Autosave revisions to keep", "slider", 20,
               "Per-document edit history depth.",
               min=0, max=100, step=5),
        ],
    },
]


def default_settings():
    out = {}
    for group in SETTINGS_SCHEMA:
        for spec in group["fields"]:
            out[spec["key"]] = spec["default"]
    out["advanced_raw"] = {}
    return out


SPECS = {
    spec["key"]: spec
    for group in SETTINGS_SCHEMA
    for spec in group["fields"]
}


def coerce_value(key, value):
    """Coerce and clamp one incoming settings value to its declared spec."""
    spec = SPECS.get(key)
    if spec is None:
        return value
    kind = spec["kind"]
    default = spec["default"]
    try:
        if kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "on", "yes")
            return bool(value)
        if kind == "slider":
            num = int(float(value))
            lo = int(spec.get("min", num))
            hi = int(spec.get("max", num))
            return max(lo, min(hi, num))
        if kind == "number":
            num = float(value)
            lo, hi = spec.get("min"), spec.get("max")
            if lo is not None:
                num = max(float(lo), num)
            if hi is not None:
                num = min(float(hi), num)
            return num
        if kind == "multiselect":
            allowed = set(spec.get("options", []))
            if isinstance(value, str):
                value = [v.strip() for v in value.split(",")]
            picked = [v for v in value if v in allowed]
            return picked or list(default)
        if kind == "select":
            options = spec.get("options")
            text = str(value).strip()
            if options and text not in options:
                return default
            return text or default
        if kind in ("device_select", "model_select"):
            # Validated against live hardware / catalogue elsewhere; accept any
            # non-empty string here so a settings file stays loadable even if a
            # model folder is later removed.
            text = str(value).strip()
            return text or default
        return str(value)
    except (TypeError, ValueError):
        return default


def parse_temperature_ladder(raw):
    """Parse the temperature fallback field into a tuple for faster-whisper."""
    if isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = str(raw).replace(";", ",").split(",")
    values = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            values.append(max(0.0, min(1.0, float(part))))
        except ValueError:
            continue
    return tuple(values) if values else (0.0,)


class SettingsStore:
    """Thread-safe JSON-backed settings with schema validation."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or PATHS.settings_file
        self._lock = threading.RLock()
        self._data = default_settings()
        self.load()

    def load(self):
        with self._lock:
            data = default_settings()
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key == "advanced_raw":
                        data[key] = value if isinstance(value, dict) else {}
                    elif key in SPECS:
                        data[key] = coerce_value(key, value)
            self._data = data
            return dict(self._data)

    def all(self):
        with self._lock:
            return dict(self._data)

    def get(self, key, fallback=None):
        with self._lock:
            return self._data.get(key, fallback)

    def update(self, incoming):
        with self._lock:
            for key, value in incoming.items():
                if key == "advanced_raw":
                    self._data[key] = value if isinstance(value, dict) else {}
                elif key in SPECS:
                    self._data[key] = coerce_value(key, value)
            self._persist()
            return dict(self._data)

    def reset(self):
        with self._lock:
            self._data = default_settings()
            self._persist()
            return dict(self._data)

    def _persist(self):
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self._path)


SETTINGS = SettingsStore()


# ---------------------------------------------------------------------------
# Offline lock enforcement
# ---------------------------------------------------------------------------

class OfflineLockError(RuntimeError):
    """Raised when code attempts a network call while the offline lock is on."""


_REAL_SOCKET_CONNECT = None
_LOOPBACK_PREFIXES = ("127.", "::1", "localhost", "0.0.0.0")


def install_offline_guard() -> None:
    """Patch socket.connect so a locked app physically cannot reach the network.

    Loopback is always permitted, since the app's own HTTP server and the
    browser talk over 127.0.0.1. Anything else raises while the lock is
    engaged, which turns the privacy promise into an enforced invariant rather
    than a claim in the About page.
    """
    global _REAL_SOCKET_CONNECT
    import socket

    if _REAL_SOCKET_CONNECT is not None:
        return
    _REAL_SOCKET_CONNECT = socket.socket.connect

    def guarded_connect(self, address):
        if SETTINGS.get("offline_lock"):
            host = ""
            if isinstance(address, tuple) and address:
                host = str(address[0])
            elif isinstance(address, str):
                host = address
            if not any(host == p or host.startswith(p) for p in _LOOPBACK_PREFIXES):
                raise OfflineLockError(
                    "Offline lock is engaged: refused outbound connection to "
                    f"{host!r}. Disable the offline lock in AI Settings if you "
                    "intend to download a model."
                )
        return _REAL_SOCKET_CONNECT(self, address)

    socket.socket.connect = guarded_connect


def instance_identity(port, token):
    return {
        "signature": APP_SIGNATURE,
        "app": APP_NAME,
        "version": app_version(),
        "pid": os.getpid(),
        "port": port,
        "token": token,
    }


def new_instance_token():
    return secrets.token_urlsafe(24)
