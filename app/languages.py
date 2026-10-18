"""Spoken languages Whisper can transcribe, the two operations, and helpers.

Local Scribe has two operations. *Transcribe* writes what was said in the
language it was said. *Translate to English* asks Whisper's translate task to
render speech in any language as English text. Nothing here translates between
other languages: the language code only tells the decoder what to expect (or,
for ``auto``, to detect it from the audio).

The code set is Whisper's own, so every value here is accepted unchanged by
both back ends (faster-whisper and mlx-whisper). Keeping the table in one place
lets the settings page, the project page, the API validation and the exporters
agree on what a language code means.
"""

from __future__ import annotations

import functools
import re

AUTO = "auto"

# A transcript whose text language could not be established. Distinct from "",
# which means "nothing recorded" (a transcript made before languages were
# stored) and is treated as the English-only app it was made by.
UNDETERMINED = "und"

# Whisper's language codes (ISO 639-1 where one exists) and English names.
LANGUAGES: dict[str, str] = {
    "en": "English", "zh": "Chinese", "de": "German", "es": "Spanish",
    "ru": "Russian", "ko": "Korean", "fr": "French", "ja": "Japanese",
    "pt": "Portuguese", "tr": "Turkish", "pl": "Polish", "ca": "Catalan",
    "nl": "Dutch", "ar": "Arabic", "sv": "Swedish", "it": "Italian",
    "id": "Indonesian", "hi": "Hindi", "fi": "Finnish", "vi": "Vietnamese",
    "he": "Hebrew", "uk": "Ukrainian", "el": "Greek", "ms": "Malay",
    "cs": "Czech", "ro": "Romanian", "da": "Danish", "hu": "Hungarian",
    "ta": "Tamil", "no": "Norwegian", "th": "Thai", "ur": "Urdu",
    "hr": "Croatian", "bg": "Bulgarian", "lt": "Lithuanian", "la": "Latin",
    "mi": "Maori", "ml": "Malayalam", "cy": "Welsh", "sk": "Slovak",
    "te": "Telugu", "fa": "Persian", "lv": "Latvian", "bn": "Bengali",
    "sr": "Serbian", "az": "Azerbaijani", "sl": "Slovenian", "kn": "Kannada",
    "et": "Estonian", "mk": "Macedonian", "br": "Breton", "eu": "Basque",
    "is": "Icelandic", "hy": "Armenian", "ne": "Nepali", "mn": "Mongolian",
    "bs": "Bosnian", "kk": "Kazakh", "sq": "Albanian", "sw": "Swahili",
    "gl": "Galician", "mr": "Marathi", "pa": "Punjabi", "si": "Sinhala",
    "km": "Khmer", "sn": "Shona", "yo": "Yoruba", "so": "Somali",
    "af": "Afrikaans", "oc": "Occitan", "ka": "Georgian", "be": "Belarusian",
    "tg": "Tajik", "sd": "Sindhi", "gu": "Gujarati", "am": "Amharic",
    "yi": "Yiddish", "lo": "Lao", "uz": "Uzbek", "fo": "Faroese",
    "ht": "Haitian Creole", "ps": "Pashto", "tk": "Turkmen", "nn": "Nynorsk",
    "mt": "Maltese", "sa": "Sanskrit", "lb": "Luxembourgish", "my": "Myanmar",
    "bo": "Tibetan", "tl": "Tagalog", "mg": "Malagasy", "as": "Assamese",
    "tt": "Tatar", "haw": "Hawaiian", "ln": "Lingala", "ha": "Hausa",
    "ba": "Bashkir", "jw": "Javanese", "su": "Sundanese", "yue": "Cantonese",
}

# Spellings people use that differ from Whisper's own names.
_ALIASES = {
    "mandarin": "zh", "castilian": "es", "valencian": "ca", "flemish": "nl",
    "burmese": "my", "myanmar (burmese)": "my", "letzeburgesch": "lb",
    "panjabi": "pa", "pushto": "ps", "sinhalese": "si", "haitian": "ht",
    "moldavian": "ro", "moldovan": "ro", "tagalog (filipino)": "tl",
    "filipino": "tl",
}

# Languages written without spaces between words. Whisper emits their word
# tokens without leading spaces, so joining them with " " would put a space
# between every character. This is the same set faster-whisper uses.
SPACELESS = frozenset({"zh", "ja", "th", "lo", "my", "yue"})

# Cantonese has a token only in the large-v3 family; older checkpoints would
# fail at decode time, so it is refused up front with a readable message.
_V3_ONLY = frozenset({"yue"})

# Languages written right to left.
RTL = frozenset({"ar", "he", "fa", "ur", "yi", "ps", "sd"})

_NAME_TO_CODE = {name.lower(): code for code, name in LANGUAGES.items()}
_NAME_TO_CODE.update(_ALIASES)


def parse(value) -> str | None:
    """Normalise user input to ``"auto"`` or a supported code.

    Accepts codes in any case (``"ES"``), regional tags (``"pt-BR"``,
    ``"zh_CN"``) and English language names (``"Spanish"``). Returns ``None``
    when the value is not recognised, so callers choose how to react.
    """
    if value is None:
        return AUTO
    text = str(value).strip().lower()
    if text in ("", AUTO, "automatic", "detect", "auto-detect"):
        return AUTO
    if text in LANGUAGES:
        return text
    if text in _NAME_TO_CODE:
        return _NAME_TO_CODE[text]
    base = text.replace("_", "-").split("-", 1)[0]
    if base in LANGUAGES:
        return base
    return None


def coerce(value) -> str:
    """Like :func:`parse` but falls back to automatic detection."""
    return parse(value) or AUTO


def name(code) -> str:
    """Display name for a code; empty for no language recorded."""
    text = str(code or "").strip().lower()
    if not text:
        return ""
    if text == AUTO:
        return "Automatic detection"
    if text == UNDETERMINED:
        return "Undetermined"
    return LANGUAGES.get(text, text)


def label(code) -> str:
    """``Spanish (es)``: the name with its code, for headers and tooltips."""
    text = str(code or "").strip().lower()
    if not text:
        return ""
    if text in LANGUAGES:
        return f"{LANGUAGES[text]} ({text})"
    return text


def choices() -> list:
    """Options for a language <select>: automatic first, then A to Z."""
    ordered = sorted(LANGUAGES.items(), key=lambda item: item[1])
    return [{"value": AUTO, "label": "Detect automatically"}] + [
        {"value": code, "label": f"{lang_name} ({code})"} for code, lang_name in ordered
    ]


def is_english(code) -> bool:
    return str(code or "").strip().lower() == "en"


def is_spaceless(code) -> bool:
    return str(code or "").strip().lower() in SPACELESS


def is_rtl(code) -> bool:
    return str(code or "").strip().lower() in RTL


def unsupported_reason(code: str, *, model_key: str = "", english_only: bool = False) -> str | None:
    """Why this model cannot transcribe this language, or None if it can."""
    if code == AUTO:
        return None
    if english_only and code != "en":
        return (
            f"The selected model ({model_key or 'English-only'}) only transcribes "
            f"English, so it cannot transcribe {name(code)}. Choose a "
            "multilingual model in Settings, or set the language to English."
        )
    if code in _V3_ONLY and "large-v3" not in (model_key or ""):
        return (
            f"{name(code)} is only supported by the Large v3 family of models. "
            "Choose Large v3 or Large v3 Turbo in Settings."
        )
    return None


def join_tokens(tokens, spaceless: bool = False, gaps=None) -> str:
    """Join word tokens into text.

    Space-delimited languages join with a single space. For languages without
    word spaces tokens are joined directly. ``gaps`` (see :func:`word_gaps`)
    says where the original text had a space; without it a space is kept only
    between two adjacent Latin or digit tokens, so an embedded English phrase
    ("我用 Zoom 开会") does not run together.
    """
    if not spaceless:
        return " ".join(t for t in tokens if t)
    out = ""
    for index, token in enumerate(tokens):
        if not token:
            continue
        if out:
            if gaps is not None and index < len(gaps):
                if gaps[index]:
                    out += " "
            elif _is_ascii_word_char(out[-1]) and _is_ascii_word_char(token[0]):
                out += " "
        out += token
    return out


def word_gaps(text: str, tokens) -> list:
    """For each token, whether a space precedes it in ``text``.

    Whisper's word tokens lose their leading space when stored, so the
    authoritative segment text is the only place the original spacing
    survives. Tokens are located in order; if one cannot be found (the text
    was edited after the words were timed) that token falls back to the
    Latin/digit rule in :func:`join_tokens` rather than guessing.
    """
    gaps: list = []
    position = 0
    for index, token in enumerate(tokens):
        found = text.find(token, position) if token else -1
        if found >= 0 and not text[position:found].strip():
            gaps.append(bool(index) and found > position)
            position = found + len(token)
        else:
            previous = tokens[index - 1] if index else ""
            gaps.append(bool(previous and token)
                        and _is_ascii_word_char(previous[-1])
                        and _is_ascii_word_char(token[0]))
    return gaps


def _is_ascii_word_char(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


# One unit of a script written without word spaces: a run of Latin letters or
# digits, or a single other character, plus any punctuation that follows it.
# Attaching trailing punctuation keeps a comma or full stop with the character
# it belongs to, whether the unit is a caption line break candidate or a word
# to align after a human edit.
SPACELESS_UNIT = re.compile(r"(?:[A-Za-z0-9'\u2019]+|[^\sA-Za-z0-9'\u2019])[^\w\s]*")


def split_units(text: str) -> list:
    """Split text into units for a script written without word spaces."""
    return SPACELESS_UNIT.findall(text or "")


def output_language(doc: dict) -> str:
    """The language the transcript *text* is actually written in.

    For a transcription this is the spoken language. For *Translate to
    English* it is English only when the text can be seen to be English:
    asking for a translation does not prove one happened (Whisper models that
    are not trained for translation simply return the spoken language). When
    the text is plainly still in the spoken language, that language is
    returned; when it cannot be told, :data:`UNDETERMINED`. ``""`` is
    returned only when nothing was recorded (older projects).
    """
    spoken = str(doc.get("language") or "").strip().lower()
    status = output_status(doc)
    if status == "original":
        return spoken
    if status in ("translated", "english_source"):
        return "en"
    if status == "untranslated":
        return spoken or UNDETERMINED
    return UNDETERMINED


def doc_is_spaceless(doc: dict) -> bool:
    """Is the transcript *text* written without word spaces?

    Decided from the language the text is written in, not the language that
    was spoken or the operation requested.
    """
    return is_spaceless(output_language(doc))


def doc_is_rtl(doc: dict) -> bool:
    return is_rtl(output_language(doc))


# ---------------------------------------------------------------------------
# Operations: Transcribe and Translate to English
# ---------------------------------------------------------------------------

TRANSCRIBE = "transcribe"
TRANSLATE = "translate"
OPERATIONS = (TRANSCRIBE, TRANSLATE)

_OPERATION_ALIASES = {
    "transcribe": TRANSCRIBE, "transcription": TRANSCRIBE,
    "translate": TRANSLATE, "translation": TRANSLATE,
    "translate to english": TRANSLATE,
}


def parse_operation(value, default: str | None = None) -> str | None:
    """Normalise an operation. Empty gives ``default``; unknown gives None."""
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return default
    return _OPERATION_ALIASES.get(text)


def operation_of(doc: dict) -> str:
    """The operation a stored transcript was produced under.

    Documents made before the operation was recorded carry it only as the
    Whisper task in their engine parameters; everything else is a
    transcription.
    """
    recorded = parse_operation(doc.get("operation"))
    if recorded:
        return recorded
    task = str((doc.get("engine") or {}).get("task") or "").strip().lower()
    return TRANSLATE if task == TRANSLATE else TRANSCRIBE


# Wording that depends on the operation. Held here, once, so the server-rendered
# pages, the JavaScript (which receives this table) and the exports cannot
# drift apart. Only labels whose meaning depends on the operation live here.
TERMS = {
    TRANSCRIBE: {
        "operation": TRANSCRIBE,
        "name": "Transcribe",
        "action": "Transcribe",
        "progressive": "Transcribing",
        "done": "Transcribed",
        "ready": "Ready to transcribe",
        "all_done": "All transcribed",
        "result": "transcript",
        "Result": "Transcript",
        "results": "transcripts",
        "download": "Download transcripts",
        "not_yet": "Not transcribed yet",
        "not_yet_body": "This recording has not been transcribed.",
        "back_link": "Back to the project to transcribe it",
        "empty_hint": "Add audio or video files above, then press Transcribe.",
        "upload_hint": "Press Transcribe when ready.",
        "output": "Text in the language spoken",
        "explain": (
            "Writes what was said, in the language it was said. Nothing is "
            "translated."
        ),
        "queued": "Queued {n} for transcription",
    },
    TRANSLATE: {
        "operation": TRANSLATE,
        "name": "Translate to English",
        "action": "Translate to English",
        "progressive": "Translating",
        "done": "Translated",
        "ready": "Ready to translate",
        "all_done": "Nothing left to translate",
        "result": "English translation",
        "Result": "English translation",
        "results": "English translations",
        "download": "Download English translations",
        "not_yet": "Not translated yet",
        "not_yet_body": "This recording has not been translated to English.",
        "back_link": "Back to the project to translate it",
        "empty_hint": "Add audio or video files above, then press Translate to English.",
        "upload_hint": "Press Translate to English when ready.",
        "output": "English text (machine translation)",
        "explain": (
            "Whisper renders speech in any language as English text. This is "
            "a machine translation, not a record of what was said, and it "
            "needs a standard multilingual model (not Turbo)."
        ),
        "queued": "Queued {n} for translation to English",
    },
}


def terms(operation) -> dict:
    return TERMS[parse_operation(operation, TRANSCRIBE) or TRANSCRIBE]


# ---------------------------------------------------------------------------
# Did the translation happen?
# ---------------------------------------------------------------------------
# Whisper's translate task is requested per run, but a model that was not
# trained for it quietly returns the spoken language instead. So the request
# proves nothing, and the text itself is checked. The check is deliberately
# small and entirely local: the script (Latin or not) plus how much of the text
# is made of very common English function words. It separates English from
# Spanish, French, Chinese and the like reliably, and reports "uncertain"
# rather than guessing on short or mixed text.

_ENGLISH_WORDS = frozenset(
    "the and of to is it that was for are with his they be this have from or "
    "had by not but what were we when your can there which she their if will "
    "would about been who its these them then than you just very because "
    "really think know like don't it's i'm that's can't i've we're they're "
    "you're didn't doesn't isn't i'd".split()
)
_WORD = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)?")
_SAMPLE_CHARS = 8000
_SAMPLE_SEGMENTS = 150


@functools.lru_cache(maxsize=256)
def assess_english(text: str) -> str:
    """``english``, ``not_english``, ``uncertain`` or ``unknown`` (too little)."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 12:
        return "unknown"
    # Basic Latin, Latin-1, Latin Extended A/B and Extended Additional.
    latin = sum(1 for c in letters if ord(c) < 0x250 or 0x1E00 <= ord(c) <= 0x1EFF)
    other = 1.0 - latin / len(letters)
    if other > 0.30:
        return "not_english"
    if other > 0.05:
        return "uncertain"
    words = _WORD.findall(text.lower().replace("\u2019", "'"))
    if len(words) < 6:
        return "unknown"
    share = sum(1 for w in words if w in _ENGLISH_WORDS) / len(words)
    if share >= 0.18:
        return "english"
    if share <= 0.07:
        return "not_english"
    return "uncertain"


def _text_sample(doc: dict) -> str:
    texts = [str(s.get("text") or "").strip() for s in doc.get("segments") or []]
    texts = [t for t in texts if t]
    if len(texts) > _SAMPLE_SEGMENTS:
        step = len(texts) / _SAMPLE_SEGMENTS
        texts = [texts[int(i * step)] for i in range(_SAMPLE_SEGMENTS)]
    return " ".join(texts)[:_SAMPLE_CHARS]


def translation_verdict(doc: dict) -> str:
    """What the text of a *Translate to English* document looks like now."""
    if operation_of(doc) != TRANSLATE:
        return "n/a"
    return assess_english(_text_sample(doc))


def output_status(doc: dict) -> str:
    """How the stored text relates to what was asked for.

    ``original``       a transcription, in the spoken language
    ``translated``     Translate to English, and the text is English
    ``english_source`` Translate to English, but the speech was English already
    ``untranslated``   Translate to English, but the text is not English
    ``unverified``     Translate to English, English could not be confirmed
    """
    if operation_of(doc) != TRANSLATE:
        return "original"
    verdict = translation_verdict(doc)
    if verdict == "not_english":
        return "untranslated"
    if str(doc.get("language") or "").strip().lower() == "en":
        return "english_source"
    if verdict == "english":
        return "translated"
    return "unverified"


def confidence_is_meaningful(doc: dict) -> bool:
    """Do Whisper's per-word probabilities say anything useful about this text?

    For a transcription they measure how sure the model was of each word it
    heard. In a machine translation they only measure how likely the model
    found each English token it generated; fluent but wrong English scores
    high, and the words no longer line up one-to-one with what was said. So
    they are not shown as a guide to accuracy for translated output. An
    English-speech document (``english_source``) is really a transcription
    and keeps them.
    """
    return output_status(doc) not in ("translated", "unverified")
