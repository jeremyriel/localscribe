# Decision: local sensitivity guardrails and future Box Sync transport

This records the classification/guardrail foundation of [R02, issue #3](https://github.com/Andrew-Copilot/localscribe/issues/3)
and the local-folder copy behavior for [R05, issue #6](https://github.com/Andrew-Copilot/localscribe/issues/6).
Local recovery snapshots are a separate R04 feature.

## Decision and revised roadmap

- **R01 — Define local backup policy and guardrails (documentation/decision).**
  R01 documents this policy, not an institutional-approval blocker for R02.
  Local Scribe makes no HIPAA, IRB, U of I, Box Health, or institutional
  approval/compliance claims. Classification is user-declared and does not
  determine whether data is PHI or provide compliance advice.
- **R02 — Per-project sensitivity declaration.** Durable values are `no_phi`,
  `may_contain_phi`, and `unknown_treat_as_phi`. New projects default to unknown.
  Missing or unrecognized stored values normalize conservatively in memory;
  the next project write persists that value. Reading/listing at startup does
  not rewrite existing projects. Incoming invalid declarations are rejected.
- **R05 — Opt-in local Box Sync copy.** The researcher configures a folder
  within their existing U of I Box Sync client and confirms that choice for
  each explicit project backup. Local Scribe copies a verified `.lsproj`
  archive; it does not use Box APIs, OAuth, or app registration. Authentication
  (including institutional Entra/SSO) belongs to the Box Sync client; Local
  Scribe does not handle credentials or tokens.

These revised entries supersede earlier roadmap wording requiring Box APIs,
OAuth/application registration, or institutional approval before implementing
local classification. The prior planning-branch backlog is not in this checkout.

## Destination policy

PHI/potential-PHI and unknown projects require an approved Box Health
destination. The initial local heuristic checks whether the selected **root
folder name** contains the literal, case-sensitive string `[Box Health - `.
Child-folder names and full paths are not root-name evidence. A matching name
can be renamed or spoofed: it is a heuristic, not Box API verification or proof
of approval. The helper does not check existence, synchronization, or availability.

After failed/inconclusive recognition, a researcher may override only by
explicitly confirming they independently verified that the selected destination
is an approved Box Health folder. This never permits ordinary Box storage for
PHI. The local helper records only the latest explicit override's timestamp,
selected destination root path, and confirmation in project metadata.
Do not supply source filenames, sensitive content, credentials, or tokens.
Generic metadata edits cannot create an override. The record is an audit
attestation, not permission for another destination or a later backup.
No override is needed or recorded for `no_phi`.

Source media, transcript text, preview audio, exports, metadata, and archives
are eligible for future backup through the approved destination. Classification
must not exclude these artifacts or imply that derived data is automatically safe.

## Out of scope

## R05 local-copy behavior and limitations

The destination is an optional absolute local folder path chosen separately for
each project on the project page (Recovery & Backup: Browse, Change, Clear, or
type a path). It is stored in a small versioned, machine-local
`backup.local.json` inside the project folder that is never exported. No folder
is selected automatically, and one project's folder never applies to another.
A folder saved by an earlier version for all projects is only offered as an
explicit "use it for this project" choice; it is not copied silently and the
old setting is left in place. Before each copy, the user must
confirm that the selected folder belongs to their existing Box Sync client.
Local Scribe does not check Box Sync installation, synchronization state,
institutional ownership, or approval; Box Sync handles its own authentication
and any later network upload. Local Scribe never calls a Box service. What is
verified is the local copy (size and SHA-256 after writing); whether Box has
uploaded it is always reported as unknown. Each project keeps a bounded local
backup history (result, local-copy verification, folder name only).

Each backup is an explicit action. It creates a versioned, checksummed project
archive, copies it to the configured folder through a temporary partial file,
and verifies the copied size and SHA-256 before publishing the final archive.
The user can cancel while the archive is being built or copied. Failed,
interrupted, or cancelled copies do not publish a final archive and may be
retried. Source media may be included or omitted.

PHI/potential-PHI and unknown projects require the existing Box Health
root-name heuristic or a fresh explicit researcher confirmation that they
independently verified this destination as approved Box Health. A stored
attestation does not authorize a later backup. `no_phi` does not require Box
Health. Offline Lock blocks local copies into Box Sync folders because the
separate Box client may upload them. Activity history records outcomes using a
hashed project reference and does not store folder paths, titles, filenames,
credentials, or transcript content.

This is not an institutional approval or compliance claim. Local folder copies
do not replace separate disaster-recovery storage. Remote backup services, Box
APIs, authentication/credential storage, retention, deletion, remote
versioning, and Box application registration remain out of scope.
Transcription behavior is unchanged. Automated tests use synthetic records
only. Manual end-to-end recovery UI testing is pending and deferred for the
project owner to perform later.
