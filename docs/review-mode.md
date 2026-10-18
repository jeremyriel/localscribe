# Review Mode

Each transcript editor includes a Review Mode queue for segments with low
word confidence, likely transcription artefacts, unassigned speakers, manual
flags, or edits made since their last review. Proofreading findings arrive as
the document's background spelling/artefact sweep checks segments; the queue's
artifact count can therefore grow while that sweep runs.

The category filters narrow the visible unresolved items without changing the
counts or completion progress. Use **Next unresolved** to jump to and play the
next item, or use a queue row to open a specific segment. Marking an item
reviewed advances to the next unresolved item. A manual flag is removed when
its queue action is cleared, and can also be toggled from a segment's flag
button. Resolving a review item records that segment as reviewed. Editing
segment text or speaker assignment clears prior resolutions and places the
segment in the changed-since-validation queue.

## Editor layout

The transcript editor puts the transcript first: a compact header (with links
to the tools below), the player and waveform, then the transcript. On windows
wider than 1000 px the tools sit in a side panel to the right; on narrower
windows they stack beneath the transcript.

Five tools are collapsible panels that start closed: **Review Mode**,
**Speakers**, **De-identification review assistance**, **Timestamped comments
and questions**, and **Help: shortcuts and highlight legend**. The links after
"Tools:" in the header open a panel and move focus to its title. Validation
sign-off, Offline review handoff, Finish the review, Download, and Document
facts stay visible beneath them.

On windows at least 761 px wide, the top navigation bar stays in view while you
scroll; if the window is also at least 600 px tall, the player and the find bar
do too. Playback shortcuts are ignored while focus is on a panel title or a
form control, so Space and the arrow keys do not trigger playback while you
operate a panel. Printing outputs the transcript only.

## Playback review controls

The waveform can be clicked or focused and operated from the keyboard. Use
**Speed** to change playback rate, **Pre-roll** and **Post-roll** to add context
when replaying a word or segment, **Replay segment** (or a segment's replay
button) to listen again, and **Loop segment** to repeat it. Zoom controls show
the full waveform or magnify around the current playhead.

Keyboard shortcuts are available when focus is outside an input, select, button,
or transcript text field:

| Key | Action |
| --- | --- |
| Space | Play or pause |
| Left / Right | Seek 2 seconds |
| Shift + Left / Right | Seek 5 seconds |
| Home / End | Seek to the beginning / end |
| R | Replay the current segment with pre/post-roll |
| L | Toggle looping the current segment |
| + / - | Zoom waveform in / out |

The waveform itself is keyboard focusable; its accessible value tracks the
playback timestamp. Clicked waveform timestamps seek within the current zoom
window, and media requests support byte ranges so seeking does not require
downloading the entire recording.

Flags and completion state are stored alongside each segment in
`transcript.json`. That file is included in Local Scribe project archives, so
review progress moves with project export, backup, and restore without a
separate sidecar file.

Timestamped comments and questions are stored in the transcript as well. Use
**Use current playhead** to anchor a note to the recording time, then add its
text and optional author. Selecting a note's timestamp returns playback to
that point. Notes can be removed while the transcript is editable.

For an offline reviewer, export a review package for this selected transcript,
optionally including its original source media and instructions. The `.lsreview`
file is an explicit file transfer, not a live connection. Import the returned
package here; if the local transcript diverged, both versions are kept for
comparison and an explicit choice. See the
[offline handoff guide](offline-handoff.md).

## De-identification review assistance

The editor can flag possible email addresses, phone numbers, Social Security
numbers, dates, IP addresses, street addresses, and web addresses, and accepts
additional user-supplied regular expressions. Findings are suggestions, not
verified identifiers. Each replacement or skip is an explicit reviewer action.
Replacements use visible tokens and can be undone while the token remains
unchanged; applying and undoing each creates a transcript revision.

The local report records flagged, resolved, and skipped findings. Replacement
mappings and this report are kept in the document's
`deidentification.local.json` sidecar, not in the transcript, and are excluded
from `.lsproj` and `.lsreview` exports. A project archive can still contain
earlier transcript revisions, which may include the pre-replacement text;
inspect the archive contents and follow your organization's approved process
before sharing.

This feature is review assistance only. It can miss identifiers and produce
false positives; it does not guarantee anonymization, de-identification, or
compliance with any law, regulation, or policy. Review the full document and
follow an appropriate privacy/compliance review process.

## Validation and export sign-off

The document workflow progresses through **Draft → Transcribed → In review →
Validated → Exported**. Begin review explicitly in the document's Validation
sign-off panel. A validation sign-off records the reviewer, local timestamp,
and notes in `transcript.json`; the audit list also records review starts,
reopenings, and exports. Reopening a validated transcript requires a reviewer,
a reason, and explicit confirmation. Editing, re-timestamping, or restoring a
validated transcript is blocked until validation is reopened.

Exports made before sign-off remain available but do not advance the workflow
past its review stage. Exporting a validated transcript records the export
time, formats, and a source fingerprint locally. Prose, Markdown, Word, JSON,
and WebVTT outputs include applicable workflow/provenance fields; SubRip has
no standard metadata channel.

The standalone model checks are in `tests/test_review.py` and can be run with
`python -m tests.test_review`.
