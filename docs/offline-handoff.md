# Offline review handoff

An offline handoff is an explicit file exchange for one selected transcript; it
does not create an account, contact a service, or synchronize changes in real
time. Export a review package from a document, optionally include its original
media, and add instructions for the reviewer. Transfer the `.lsreview` file
using a method approved for the data. The package is a portable copy, not
encrypted or signed; anyone who receives it can read its contents.

## Package contents

The ZIP-based format is versioned independently from `.lsproj`:

```text
manifest.json
base_transcript.json
transcript.json
annotations.json
instructions.txt
media/<original-filename>       (only when requested)
```

`manifest.json` identifies the project and selected document, records whether
media was requested, and lists every payload's byte count and SHA-256 digest.
Hashes detect payload changes against the manifest but do not authenticate the
author. `base_transcript.json` is the handoff baseline used for conflict
detection. Reviewers add timestamped notes in `annotations.json` or the
transcript's `annotations` array and may edit `transcript.json`; the package
may be edited directly and returned without recalculating the checksums for
those two reviewer-editable files. The baseline, instructions, and optional
media remain checksum-verified. `instructions.txt` is plain text. Imported
annotations are merged by stable ID and kept with the
canonical `transcript.json`; each has a comment/question type, recording time,
creation timestamp, and optional author.

The local de-identification report and reversible replacement map are not part
of an `.lsreview` package. Only the current transcript is included, so review
any findings and replacements before creating a handoff. This assistance can
miss identifiers or flag ordinary text and is not a compliance guarantee.

The included media is the original source file, not a real-time stream. Media
is excluded by default. Do not include it unless the reviewer needs it and the
transfer destination is approved for that data.

## Import and conflicts

Import a returned package from the same document's editor. The application
verifies its file list and checksums and rejects packages for another project
or recording. If the active transcript still matches the handoff baseline, the
reviewed transcript is applied and the prior version remains in edit history.
Comments and questions are merged even when the transcript itself conflicts.

If both sides changed the transcript, import does not overwrite the active
version. Local, incoming, and baseline transcripts are stored under that
document's `conflicts/` folder. Compare the downloadable current and imported
versions, then explicitly choose **Keep current** or **Use imported** in the
editor. The chosen transcript becomes active; all three saved versions remain
available in the conflict folder. A resolved conflict is marked with the
choice and time.

Imports and annotations respect the transcript validation lock; reopen a
validated transcript before applying returned work. To send further work,
export a new package after resolution. There is no live merge, background
transfer, or synchronization between copies.

The offline handoff checks are in `tests/test_handoff.py`.
