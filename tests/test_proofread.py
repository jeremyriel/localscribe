"""Tests for the spelling and transcription-artefact checks.

The guiding property: on a verbatim speech transcript, the checker must stay
quiet about how people talk and speak up only about how the text was written
down. A checker that flags "um" or "gonna" is worse than no checker, because
it buries the real errors.

Run with:  .venv/Scripts/python.exe -m tests.test_proofread
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import proofread as P

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def flagged(text, **kw):
    """Words flagged as misspelled."""
    return [i["word"] for i in P.check_text(text, artefacts=False, **kw)]


def artefacts(text, **kw):
    """Artefact codes raised."""
    return [i["code"] for i in P.check_text(text, spelling=False, **kw)]


def test_dictionary_loads():
    check("the dictionary file ships with the app", P.DICTIONARY.available,
          str(P.WORDLIST))
    size = len(P.DICTIONARY)
    check("it holds a substantial vocabulary", size > 100_000, f"{size:,} words")
    check("the transcript supplement is merged",
          P.DICTIONARY.source_counts.get("supplement", 0) > 40,
          str(P.DICTIONARY.source_counts))


def test_ordinary_prose_is_clean():
    text = ("Thank you for agreeing to take part in this interview today. "
            "I want to start by asking about your experience with the "
            "programme and how you first came to hear about it.")
    check("well-formed prose raises nothing", flagged(text) == [], str(flagged(text)))
    check("and no artefacts either", artefacts(text) == [], str(artefacts(text)))


def test_speech_fillers_are_not_flagged():
    """The property that decides whether this feature is usable at all."""
    text = ("Um, yeah, so, uh, I mean, it was kinda like, mm-hmm, you know, "
            "gonna be tricky. Erm, uhh, mmm, yeah, alrighty. Ohh, welp. "
            "I was just talkin and thinkin about it, dunno really.")
    bad = flagged(text)
    check("hesitations and colloquialisms pass clean", bad == [],
          f"wrongly flagged: {bad}")


def test_contractions_and_possessives():
    text = ("It's the teacher's view that they're doing what we've always "
            "done, and he'd say the students' work isn't finished.")
    check("contractions and possessives pass", flagged(text) == [],
          str(flagged(text)))
    curly = "It’s the teacher’s view that they’re fine."
    check("curly apostrophes are handled", flagged(curly) == [],
          str(flagged(curly)))


def test_real_misspellings_are_caught():
    text = "The studnets recieved thier assignmnets on Wendsday."
    bad = flagged(text)
    check("genuine misspellings are caught", len(bad) >= 4, str(bad))
    check("'studnets' is flagged", "studnets" in bad, str(bad))

    issues = P.check_text("The studnets were happy.", artefacts=False)
    suggestions = issues[0]["suggestions"] if issues else []
    check("a correction is suggested", "students" in suggestions, str(suggestions))

    issues = P.check_text("She recieved it.", artefacts=False)
    sug = issues[0]["suggestions"] if issues else []
    check("suggestions are offered for a common slip", "received" in sug, str(sug))


def test_offsets_are_exact():
    text = "The studnets arrived."
    issue = P.check_text(text, artefacts=False)[0]
    check("the offsets select exactly the bad word",
          text[issue["start"]:issue["end"]] == "studnets",
          repr(text[issue["start"]:issue["end"]]))


def test_case_is_preserved_in_suggestions():
    issues = P.check_text("Studnets arrived.", artefacts=False)
    sug = issues[0]["suggestions"] if issues else []
    check("suggestions match the original capitalisation",
          any(s == "Students" for s in sug), str(sug))


def test_short_tokens_and_numbers_skipped():
    text = "J. K. said 3pm, covid19 and the A4 form are OK."
    check("initials, numerals and short tokens are ignored",
          flagged(text) == [], str(flagged(text)))


def test_glossary_words_accepted():
    text = "The TRAILblazer study with Riel at UIC used NGSS framing."
    before = flagged(text)
    check("unknown project terms are flagged without a glossary",
          len(before) > 0, str(before))

    extra = P.extra_words("TRAILblazer, Riel, UIC, NGSS")
    after = flagged(text, extra=extra)
    check("the project glossary silences them", after == [], str(after))


def test_hyphenated_compounds():
    text = "It was a well-being focused, evidence-based, mm-hmm sort of thing."
    check("hyphenated compounds of known parts pass", flagged(text) == [],
          str(flagged(text)))


def test_doubled_word_artefact():
    codes = artefacts("I went to to the shop.")
    check("a doubled word is caught", "doubled-word" in codes, str(codes))
    check("legitimate doubles are left alone",
          "doubled-word" not in artefacts("I had had enough of that."),
          str(artefacts("I had had enough of that.")))


def test_repeated_phrase_artefact():
    looped = ("and so we went there and so we went there and that was it.")
    codes = artefacts(looped)
    check("a repeated phrase loop is caught", "repeated-phrase" in codes, str(codes))
    check("ordinary prose is not mistaken for a loop",
          "repeated-phrase" not in artefacts(
              "We went there and then we came back again later."))


def test_a_an_artefact():
    check("'a apple' is caught", "a-an" in artefacts("I ate a apple."))
    check("'an teacher' is caught", "a-an" in artefacts("She is an teacher."))
    check("'an hour' is accepted", "a-an" not in artefacts("It took an hour."),
          str(artefacts("It took an hour.")))
    check("'a university' is accepted",
          "a-an" not in artefacts("He went to a university."),
          str(artefacts("He went to a university.")))


def test_known_slips():
    codes = artefacts("I could of gone, and alot of people definately agreed.")
    check("'could of' is caught", "known-slip" in codes, str(codes))
    issues = P.check_text("I could of gone.", spelling=False)
    check("a correction is offered",
          any("could have" in i["suggestions"] for i in issues), str(issues))


def test_spacing_and_capitalisation():
    check("space before punctuation is caught",
          "spacing" in artefacts("Well , that was odd."))
    check("a lower-case sentence start is caught",
          "capitalisation" in artefacts("That was odd. then we left."))
    check("a normal sentence break is fine",
          "capitalisation" not in artefacts("That was odd. Then we left."))


def test_disabling_layers():
    text = "The studnets could of gone."
    check("spelling can be switched off",
          all(i["kind"] == "artefact"
              for i in P.check_text(text, spelling=False)))
    check("artefacts can be switched off",
          all(i["kind"] == "spelling"
              for i in P.check_text(text, artefacts=False)))


def test_segment_checking_and_cache():
    segments = [
        {"id": 0, "text": "The studnets arrived."},
        {"id": 1, "text": "Everything here is fine."},
    ]
    result = P.check_segments(segments)
    check("results are keyed by segment id", set(result) == {"0", "1"}, str(set(result)))
    check("the bad segment has an issue", len(result["0"]) == 1, str(result["0"]))
    check("the clean segment has none", result["1"] == [], str(result["1"]))

    again = P.check_segments(segments)
    check("a repeat check returns the same result", again == result)


def test_empty_input():
    check("empty text yields nothing", P.check_text("") == [])
    check("whitespace yields nothing", P.check_text("   \n ") == [])
    check("no segments yields nothing", P.check_segments([]) == {})


def main() -> int:
    tests = [
        test_dictionary_loads,
        test_ordinary_prose_is_clean,
        test_speech_fillers_are_not_flagged,
        test_contractions_and_possessives,
        test_real_misspellings_are_caught,
        test_offsets_are_exact,
        test_case_is_preserved_in_suggestions,
        test_short_tokens_and_numbers_skipped,
        test_glossary_words_accepted,
        test_hyphenated_compounds,
        test_doubled_word_artefact,
        test_repeated_phrase_artefact,
        test_a_an_artefact,
        test_known_slips,
        test_spacing_and_capitalisation,
        test_disabling_layers,
        test_segment_checking_and_cache,
        test_empty_input,
    ]
    for test in tests:
        print(f"\n{test.__name__}")
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
