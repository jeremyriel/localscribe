# Accessibility review baseline

Local Scribe is a local browser interface, not a certified accessible product.
This checklist sets a minimum review baseline; automated checks do not replace
keyboard and assistive-technology testing.

## Automated checks

Run `python -m tests.test_accessibility` for static checks covering:

- Skip navigation, named landmarks, active-page state, and transcript controls.
- Keyboard speaker assignment and visible speaker identifiers that do not rely
  on color.
- Visible focus, scalable base typography, forced-colors and reduced-motion
  preferences, and 4.5:1 contrast for muted text on the standard surfaces.

## Required PR and release review

Before a release, reviewers should verify the main workflows using only a
keyboard: create/open a project, add and transcribe a recording, edit/review
segments, assign speakers, validate, export, restore a snapshot, and complete
the important confirmation prompts. Check that focus remains visible and
returns to a useful control after actions.

Also inspect the project and transcript pages at 200% text zoom and a narrow
viewport; verify controls do not overlap or require horizontal scrolling for
ordinary tasks. Test at least one supported screen reader on each release
platform (VoiceOver on macOS, Narrator on Windows, and Orca on Linux where
available), including the waveform's name/value announcements, live status
updates, labels, and confirmation prompts. Check high-contrast/forced-colors
mode and reduced-motion preference. Record findings and unresolved limitations
in the PR or release notes.

## Known limits

The waveform is a keyboard-operated slider, but visual waveform details are not
currently represented as an audio-description track. Browser-native confirmation
prompts are used for destructive decisions. These limitations require human
review and should not be described as fully conformant without testing.
