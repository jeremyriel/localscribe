# Language support boundary

Whisper models labeled **multilingual** can recognize many languages. That
catalogue label describes model capability; it is not a claim that Local Scribe
has validated the full application workflow for those languages.

## Current status

| Area | Status |
|---|---|
| End-to-end validated workflow | English |
| Language detection and selection | Passed through to the selected Whisper model; capability varies by model |
| Transcript storage and exports | UTF-8; a synthetic Spanish fixture checks language metadata and Unicode through SRT, VTT, DOCX, JSON, and `.lsproj` |
| Proofreading | English-only dictionary and artefact rules; disabled for non-English or unknown-language transcripts |
| Speaker workflow | Human-created roster and reversible manual turn assignment; no automatic diarization |

The synthetic Spanish fixture verifies character preservation and data flow only.
It does not measure transcription accuracy, validate Spanish proofreading, or
establish general Spanish support. Users should not treat model capability as
language-level validation. A language should be described as supported only
after representative non-sensitive fixtures cover transcription, proofreading,
exports, and the user interface.

## Speaker assignment

Speaker identification remains a human task. Reviewers can create and name
speakers, assign complete speaking turns with the visible roster controls or
keyboard number shortcuts, and clear or change assignments. Local Scribe does
not infer identities or automatically diarize speakers. Any future experimental
diarization must be optional, confidence-marked, reversible, and confirmed by a
human before it changes a transcript.
