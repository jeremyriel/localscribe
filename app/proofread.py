"""Proofreading: spelling and transcription-artefact checks.

Two layers, matching what the editor paints:

* **spelling** (red) -- a word not found in the dictionary, the transcript
  supplement, the project glossary, or the document's accepted words.
* **artefact** (blue) -- a narrow set of high-precision checks for mistakes
  the *transcriber* made, not mistakes the speaker made.

That second distinction is the important one. These are verbatim records of
people talking, and people talk in fragments, false starts and comfortable
ungrammaticality. A general grammar checker flags all of it, which buries the
real errors and, worse, invites a validator to "tidy" the data -- destroying
exactly the fidelity that makes a transcript worth having. So nothing here
judges the speaker's grammar. Every check below fires only where the text is
almost certainly wrong on the page rather than odd in the mouth.

Checking runs server-side so the dictionary is loaded once, sits next to the
project glossary, and never has to be shipped to the browser. Results are
cached per segment text, so reopening a document is instant and typing
re-checks only the segment being edited.
"""

from __future__ import annotations

import gzip
import re
import threading
from pathlib import Path

from .config import PATHS

DICT_DIR = PATHS.root / "app" / "data" / "dictionary"
WORDLIST = DICT_DIR / "en_US.txt.gz"
SUPPLEMENT = DICT_DIR / "transcript-supplement.txt"

# Words shorter than this are never flagged: initials, "a", "I", and the
# letter-by-letter spelling people do on recordings would all be noise.
MIN_WORD_LENGTH = 3

MAX_SUGGESTIONS = 5
CACHE_LIMIT = 4000

# A token: letters, with internal apostrophes and hyphens kept attached, so
# "don't" and "well-being" arrive whole. Curly apostrophes are normalised
# first, because Whisper emits them and the dictionary stores straight ones.
_TOKEN = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s+")

_TRAILING_SUFFIXES = ("'s", "s'", "'ll", "'ve", "'re", "'d", "'m")


class Dictionary:
    """The word set, loaded once and shared."""

    def __init__(self) -> None:
        self._words: set = set()
        self._loaded = False
        self._lock = threading.Lock()
        self.source_counts: dict = {}

    def load(self) -> set:
        with self._lock:
            if self._loaded:
                return self._words

            words: set = set()
            if WORDLIST.exists():
                with gzip.open(WORDLIST, "rt", encoding="utf-8") as handle:
                    words.update(
                        line.strip().lower() for line in handle if line.strip()
                    )
            self.source_counts["base"] = len(words)

            supplement = 0
            if SUPPLEMENT.exists():
                for line in SUPPLEMENT.read_text(encoding="utf-8").splitlines():
                    word = line.strip().lower()
                    if word and not word.startswith("#"):
                        words.add(word)
                        supplement += 1
            self.source_counts["supplement"] = supplement

            self._words = words
            self._loaded = True
            return self._words

    @property
    def available(self) -> bool:
        return WORDLIST.exists()

    def __len__(self) -> int:
        return len(self.load())


DICTIONARY = Dictionary()


def normalise_token(token: str) -> str:
    return token.replace("’", "'").strip("'-").lower()


def extra_words(*sources) -> set:
    """Build the per-request allow-list from glossary and accepted words."""
    extra: set = set()
    for source in sources:
        if not source:
            continue
        if isinstance(source, str):
            items = re.split(r"[,\n;]+", source)
        else:
            items = list(source)
        for item in items:
            for token in _TOKEN.findall(str(item)):
                cleaned = normalise_token(token)
                if cleaned:
                    extra.add(cleaned)
    return extra


def known(word: str, words: set, extra: set = frozenset()) -> bool:
    """Is this token spelled correctly?"""
    cleaned = normalise_token(word)
    if not cleaned or len(cleaned) < MIN_WORD_LENGTH:
        return True
    if any(ch.isdigit() for ch in cleaned):
        return True     # "3pm", "covid19": not a spelling matter
    if cleaned in words or cleaned in extra:
        return True

    # Possessives and contractions whose stem is known.
    for suffix in _TRAILING_SUFFIXES:
        if cleaned.endswith(suffix):
            stem = cleaned[: -len(suffix)]
            if stem in words or stem in extra:
                return True

    # Hyphenated compounds: accept when every part is known, which is how
    # "well-being" and "mm-hmm" pass without listing every combination.
    if "-" in cleaned:
        parts = [p for p in cleaned.split("-") if p]
        if parts and all(
            p in words or p in extra or len(p) < MIN_WORD_LENGTH for p in parts
        ):
            return True

    return False


def suggest(word: str, words: set, limit: int = MAX_SUGGESTIONS) -> list:
    """Candidate corrections within edit distance 1, then 2 if needed.

    Generating candidates and testing membership is far cheaper than scanning
    120,000 words, so this stays fast enough to run inline.
    """
    target = normalise_token(word)
    if not target:
        return []

    alphabet = "abcdefghijklmnopqrstuvwxyz'"

    def edits(text: str) -> set:
        splits = [(text[:i], text[i:]) for i in range(len(text) + 1)]
        out = set()
        for left, right in splits:
            if right:
                out.add(left + right[1:])                        # deletion
                if len(right) > 1:
                    out.add(left + right[1] + right[0] + right[2:])   # swap
                for ch in alphabet:
                    out.add(left + ch + right[1:])               # replacement
            for ch in alphabet:
                out.add(left + ch + right)                       # insertion
        return out

    first = {w for w in edits(target) if w in words}
    if len(first) >= limit:
        ranked = sorted(first, key=lambda w: (abs(len(w) - len(target)), w))
        return _match_case(word, ranked[:limit])

    second: set = set()
    if len(target) <= 12:
        for candidate in edits(target):
            second.update(w for w in edits(candidate) if w in words)

    ranked = sorted(first) + sorted(second - first)
    ranked.sort(key=lambda w: (w not in first, abs(len(w) - len(target)), w))
    return _match_case(word, ranked[:limit])


def _match_case(original: str, candidates: list) -> list:
    """Return suggestions capitalised like the word they replace."""
    if original[:1].isupper():
        if original.isupper() and len(original) > 1:
            return [c.upper() for c in candidates]
        return [c.capitalize() for c in candidates]
    return list(candidates)


# ---------------------------------------------------------------------------
# Artefact checks
# ---------------------------------------------------------------------------
# Each returns [(start, end, code, message, suggestions)] over the given text.

KNOWN_SLIPS = {
    "could of": "could have",
    "would of": "would have",
    "should of": "should have",
    "must of": "must have",
    "might of": "might have",
    "alot": "a lot",
    "definately": "definitely",
    "seperate": "separate",
    "recieve": "receive",
    "occured": "occurred",
    "untill": "until",
    "alright": "all right",
}

# Words that legitimately repeat in English, so a doubling is not an artefact.
LEGITIMATE_DOUBLES = {
    "had", "that", "very", "no", "so", "very", "really", "long", "many",
    "blah", "ha", "yeah", "well", "now", "sure", "bye",
}

_VOWEL_SOUND = re.compile(r"^[aeiou]", re.I)
# Words starting with a vowel letter but a consonant sound, and the reverse.
_AN_EXCEPTIONS = {"one", "once", "university", "unique", "union", "united",
                  "user", "usual", "usually", "european", "euro", "unit"}
_A_EXCEPTIONS = {"hour", "honest", "honour", "honor", "heir", "honestly"}


def check_doubled_words(text: str) -> list:
    """`the the` -- a very common Whisper artefact at chunk boundaries.

    A run of three or more is reported as one issue spanning the whole run,
    rather than as overlapping pairs: "the the the the" is a single mistake to
    fix, and overlapping highlights would be unreadable.
    """
    tokens = [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(text)]
    issues = []
    index = 0

    while index < len(tokens):
        word, start, end = tokens[index]
        lowered = word.lower()

        run = index
        while (run + 1 < len(tokens)
               and tokens[run + 1][0].lower() == lowered):
            run += 1

        repeats = run - index + 1
        if repeats > 1 and lowered not in LEGITIMATE_DOUBLES:
            issues.append((
                start, tokens[run][2], "doubled-word",
                f"'{word}' appears {repeats} times in a row."
                if repeats > 2 else f"'{word}' appears twice in a row.",
                [word],
            ))

        index = run + 1
    return issues


MIN_LOOP_WORDS = 3
MAX_LOOP_WORDS = 10


def check_repeated_phrase(text: str) -> list:
    """A multi-word phrase repeated back to back: a hallucination loop.

    Whisper's decoder can get stuck and emit the same phrase repeatedly, and
    the repeated unit is not a fixed length -- it may be three words or nine.
    So every window size in range is tried, longest first, because reporting
    "and so we went there" once is more useful than reporting a three-word
    fragment of it twice.
    """
    tokens = [(m.group(0).lower(), m.start(), m.end()) for m in _TOKEN.finditer(text)]
    if len(tokens) < MIN_LOOP_WORDS * 2:
        return []

    issues = []
    index = 0
    while index < len(tokens):
        matched = 0
        for size in range(min(MAX_LOOP_WORDS, (len(tokens) - index) // 2),
                          MIN_LOOP_WORDS - 1, -1):
            first = [t[0] for t in tokens[index:index + size]]
            second = [t[0] for t in tokens[index + size:index + size * 2]]
            if first != second:
                continue

            # Count how many times the phrase repeats in total.
            repeats = 2
            probe = index + size * 2
            while probe + size <= len(tokens) and \
                    [t[0] for t in tokens[probe:probe + size]] == first:
                repeats += 1
                probe += size

            start = tokens[index][1]
            end = tokens[probe - 1][2]
            phrase = " ".join(first)
            issues.append((
                start, end, "repeated-phrase",
                f"The phrase '{phrase}' repeats {repeats} times in a row. "
                "Whisper sometimes loops like this; check it against the audio.",
                [phrase],
            ))
            matched = probe - index
            break

        index += matched if matched else 1
    return issues


def check_a_an(text: str) -> list:
    issues = []
    tokens = [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(text)]
    for (word, start, end), (nxt, _ns, _ne) in zip(tokens, tokens[1:]):
        lowered = word.lower()
        if lowered not in ("a", "an"):
            continue
        following = nxt.lower()
        if following in _AN_EXCEPTIONS:
            wants_an = False
        elif following in _A_EXCEPTIONS:
            wants_an = True
        else:
            wants_an = bool(_VOWEL_SOUND.match(following))

        if wants_an and lowered == "a":
            issues.append((start, end, "a-an",
                           f"'a {nxt}' should probably be 'an {nxt}'.",
                           ["an" if word[0].islower() else "An"]))
        elif not wants_an and lowered == "an":
            issues.append((start, end, "a-an",
                           f"'an {nxt}' should probably be 'a {nxt}'.",
                           ["a" if word[0].islower() else "A"]))
    return issues


def check_known_slips(text: str) -> list:
    issues = []
    for wrong, right in KNOWN_SLIPS.items():
        for match in re.finditer(rf"\b{re.escape(wrong)}\b", text, re.I):
            issues.append((match.start(), match.end(), "known-slip",
                           f"'{match.group(0)}' is usually '{right}'.", [right]))
    return issues


def check_spacing(text: str) -> list:
    issues = []
    for match in re.finditer(r"\s+([,.;:!?])", text):
        issues.append((match.start(), match.end(), "spacing",
                       "There is a space before this punctuation.",
                       [match.group(1)]))
    for match in re.finditer(r"\S(  +)\S", text):
        issues.append((match.start(1), match.end(1), "spacing",
                       "Repeated spaces.", [" "]))
    return issues


def check_capitalisation(text: str) -> list:
    """A sentence starting lower-case, which usually means a lost capital."""
    issues = []
    for match in _SENTENCE_END.finditer(text):
        following = _TOKEN.search(text, match.end())
        if not following or following.start() != match.end():
            continue
        word = following.group(0)
        if word[:1].islower() and word.lower() not in ("i",):
            issues.append((following.start(), following.end(), "capitalisation",
                           f"'{word}' starts a sentence but is not capitalised.",
                           [word.capitalize()]))
    return issues


ARTEFACT_CHECKS = (
    check_doubled_words,
    check_repeated_phrase,
    check_a_an,
    check_known_slips,
    check_spacing,
    check_capitalisation,
)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def check_text(
    text: str,
    *,
    extra: set = frozenset(),
    spelling: bool = True,
    artefacts: bool = True,
    suggestions: int = MAX_SUGGESTIONS,
) -> list:
    """Return issues for one segment as character offsets into `text`.

    Each issue is {start, end, kind, code, word, message, suggestions}, where
    `kind` is "spelling" or "artefact" -- the two squiggle colours.
    """
    if not text or not text.strip():
        return []

    issues: list = []

    if spelling:
        words = DICTIONARY.load()
        for match in _TOKEN.finditer(text):
            word = match.group(0)
            if known(word, words, extra):
                continue
            issues.append({
                "start": match.start(),
                "end": match.end(),
                "kind": "spelling",
                "code": "misspelling",
                "word": word,
                "message": f"'{word}' is not in the dictionary.",
                "suggestions": suggest(word, words, suggestions) if suggestions else [],
            })

    if artefacts:
        for check in ARTEFACT_CHECKS:
            for start, end, code, message, fixes in check(text):
                issues.append({
                    "start": start,
                    "end": end,
                    "kind": "artefact",
                    "code": code,
                    "word": text[start:end],
                    "message": message,
                    "suggestions": list(fixes)[:suggestions],
                })

    issues.sort(key=lambda i: (i["start"], i["end"]))
    return issues


class ResultCache:
    """Per-segment results keyed by the text that produced them."""

    def __init__(self, limit: int = CACHE_LIMIT) -> None:
        self._data: dict = {}
        self._order: list = []
        self._limit = limit
        self._lock = threading.Lock()

    def key(self, text: str, signature: str) -> str:
        import hashlib
        digest = hashlib.sha1(f"{signature}\x00{text}".encode("utf-8"))
        return digest.hexdigest()

    def get(self, key: str):
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value) -> None:
        with self._lock:
            if key not in self._data:
                self._order.append(key)
                if len(self._order) > self._limit:
                    for stale in self._order[: len(self._order) - self._limit]:
                        self._data.pop(stale, None)
                    self._order = self._order[-self._limit:]
            self._data[key] = value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._order.clear()


CACHE = ResultCache()


def check_segments(
    segments,
    *,
    extra: set = frozenset(),
    spelling: bool = True,
    artefacts: bool = True,
    suggestions: int = MAX_SUGGESTIONS,
) -> dict:
    """Check many segments. `segments` is [{"id": n, "text": "..."}]."""
    signature = f"{spelling}:{artefacts}:{suggestions}:{hash(frozenset(extra))}"
    out: dict = {}
    for item in segments or []:
        try:
            seg_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or "")
        cache_key = CACHE.key(text, signature)
        cached = CACHE.get(cache_key)
        if cached is None:
            cached = check_text(
                text, extra=extra, spelling=spelling,
                artefacts=artefacts, suggestions=suggestions,
            )
            CACHE.put(cache_key, cached)
        out[str(seg_id)] = cached
    return out


def status() -> dict:
    return {
        "available": DICTIONARY.available,
        "words": len(DICTIONARY) if DICTIONARY.available else 0,
        "sources": DICTIONARY.source_counts,
        "path": str(WORDLIST),
    }
