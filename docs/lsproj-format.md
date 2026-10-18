# Local Scribe project interchange format (`.lsproj`)

**Status:** format version 2 is the current export format. Version 1 remains
importable for backward compatibility.

An `.lsproj` file is a ZIP archive containing a project tree at its root and a
`localscribe-manifest.json` entry at the root. Archives are portable project
copies, not signed documents: hashes detect accidental or deliberate payload
changes relative to the manifest, but do not identify or authenticate who made
the archive.

## Payload layout

The archive uses paths relative to its root; it must not contain absolute paths,
parent-directory (`..`) components, or duplicate paths. A typical tree is:

```text
localscribe-manifest.json
project.json
media/<document-id>__<original-filename>
documents/<document-id>/
  document.json
  transcript.json
  revisions/<timestamp>.json
  outputs/<exported-transcript>
```

`project.json`, document records, and transcript JSON use the application's
regular on-disk schemas. Transient caches, logs, `waveform.json`, and temporary
files are not interchange payload. Browser preview audio is omitted when source
media is omitted.

The local de-identification report and reversible replacement map
(`deidentification.local.json`) and the per-project backup folder and history
(`backup.local.json`) are explicitly excluded from `.lsproj` exports and
snapshots. The importer skips any `*.local.json` entry and drops a legacy
`box_health_override` record from `project.json`, so an archive can never
restore a backup folder, history, or earlier destination attestation. Replacing
a project keeps this computer's own `backup.local.json` (without its
attestation).
Transcript revisions remain part of the project tree and can contain earlier,
unreplaced text; review the archive before sharing it.

## Version 2 manifest

The manifest is UTF-8 JSON with these fields:

| Field | Meaning |
|---|---|
| `format` | Constant string `localscribe-project`. |
| `format_version` | Integer `2`. |
| `app_version` | Local Scribe application version that wrote the export. |
| `exported` | Export timestamp in ISO 8601 form, including its UTC offset. |
| `slug`, `project_id`, `title` | Project identity and display metadata. |
| `includes_media` | Boolean: whether source-media files were requested for this export. |
| `data_sensitivity` | User-declared sensitivity value copied from project metadata. |
| `documents` | Array of each document's `id`, `filename`, and `status`. |
| `transcription_provenance` | Array keyed by `document_id`, with the document's `engine`, optional `provenance`, `media`, and `stats` metadata. |
| `files` | One entry for every payload file other than the manifest itself. |

Each `files` entry has a unique root-relative `path`, its uncompressed
`size_bytes`, and lowercase hexadecimal SHA-256 `sha256` digest. The list must
exactly match the archive's payload files. Directory entries are not payload
files and are not listed. `integrity_verified` is a runtime result and is not a
serialized manifest field.

Example (the `files` array is abbreviated):

```json
{
  "format": "localscribe-project",
  "format_version": 2,
  "app_version": "1.2.0",
  "exported": "2026-01-01T12:00:00-06:00",
  "slug": "study",
  "project_id": "project-identifier",
  "title": "Study",
  "includes_media": false,
  "data_sensitivity": "unknown_treat_as_phi",
  "documents": [
    {"id": "document-id", "filename": "interview.wav", "status": "transcribed"}
  ],
  "transcription_provenance": [
    {
      "document_id": "document-id",
      "engine": {"name": "engine name"},
      "provenance": {
        "vocabulary_profile": {"id": "profile-id", "name": "Study terms"},
        "transcription_settings": {"language": "en"}
      },
      "media": {},
      "stats": {}
    }
  ],
  "files": [
    {
      "path": "project.json",
      "size_bytes": 123,
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ]
}
```

The sample digest and byte count are illustrative. An implementation must
calculate them from the exact bytes stored in the archive.

## Media and transcript-only exports

When `includes_media` is `false`, source files under `media/` and document
preview files are omitted. Document metadata and transcripts remain in the
archive. The document's `media_path` can therefore refer to a source file that
is not present. On import, Local Scribe reports that the archive is
transcript-only, identifies document records whose media is missing, and keeps
their transcripts available. The project view marks those recordings and does
not offer transcription until the source recording is available again.

`includes_media` describes the export choice; it is not proof that every
expected media file was present. Consumers should inspect the actual files.

## Integrity and provenance

For version 2, import verifies the manifest file list, payload sizes, and
SHA-256 hashes before extraction, and checks payload bytes again while copying
them into the project. An invalid or incomplete file list or checksum rejects
the archive without replacing an existing project. The import result and
project view expose the integrity outcome.

Version 1 has no required checksum list. Its payload can be imported, but its
integrity cannot be verified; Local Scribe identifies this explicitly. Hashes
are not a digital signature and do not establish provenance. The manifest's
transcription provenance records the available engine, optional profile and
settings metadata, media-probe, and statistics copied from each document.
Vocabulary prompt and hotword values are omitted from engine metadata by
default. Profile terms remain in the local profile library and are not bundled
in project exports. The original project metadata also retains its application
version where present.

## Versioning and migration policy

`format_version` is an integer describing the archive interchange schema, not
the application release. A missing `format_version` is interpreted as version
1 for compatibility. Version 1 archives are imported with conservative
defaults: a missing project ID is generated and missing sensitivity is treated
as the application's default. No checksum assurance is inferred for v1.

Version 2 is the current write format. Import preserves the project tree and
transcript JSON while normalizing project identity, slug, and sensitivity as
needed; the application does not rewrite transcript content as a separate
migration. Future readers must explicitly migrate older data or preserve it,
and must reject unsupported future versions rather than guessing their
semantics. Format changes that alter required fields or interpretation require
a new integer version and a documented migration path. Keep versioned golden
archives in `tests/fixtures/` and test their import behavior when changing
readers or writers.
