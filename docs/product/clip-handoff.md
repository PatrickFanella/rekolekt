# Clip provenance handoff

A completed clip export can be described by a small JSON document that an editor such as clpr reads alongside the MP4. It records which recording, range and transcript the owner selected, and the hash of the rendered file. It carries no credentials, so the owner can pass it on with the clip.

`GET /clips/{job_id}/handoff` returns it to the export's owner (`Cache-Control: private, no-store`). The example in [`docs/api/examples/clip-handoff.v1.json`](../api/examples/clip-handoff.v1.json) validates against the contract and is safe to use as a test fixture.

## When the transcript is captured

The transcript is snapshotted when the export is requested, not when the handoff is read. `POST /clips` accepts an optional `transcript_source` (`whisper` or `youtube`). Without it, the native Whisper transcript is used when it has segments, then captions, the same order as the public passage page. Choosing a source that has no segments returns 422. If neither source exists, the export still runs and the handoff says `"status": "unavailable"`.

The snapshot is stored in the private job record. A later edit, re-transcription or caption refresh does not change an existing handoff. A new request after such a change has a different transcript revision, so it creates a new export with its own snapshot instead of reusing the old one.

## Fields

| Field | Meaning |
| --- | --- |
| `schema`, `schema_version` | `rekolekt.clip-handoff`, version `1`. |
| `export_id`, `generated_at` | The clip job and when this document was produced (Unix seconds). |
| `archive` | Site name and the configured public origin (`FRONTEND_ORIGIN`). |
| `recording` | Archive `video_id` and the YouTube ID recorded at request time. |
| `passage` | Canonical share URL, player URL, absolute `start_ms`/`end_ms` and duration. |
| `transcript` | `status`; for a captured transcript, `source`, `transcript_id` (null for older Whisper rows with no transcript record), `language`, `kind` (`machine`, `caption-auto` or `caption-manual`), `segment_count`, `revision_sha256`, `timing` and `captured_at`. |
| `captions` | Cues clipped to the selection, with times relative to the clip start, plus `cues_truncated` and `text_sha256`. |
| `media` | `video/mp4`, filename, byte size and SHA-256 of the rendered file. |
| `provenance` | `basis: "declared"`, a fixed statement and the media source: `youtube-range` with its YouTube ID, or `registered-original` with the original's SHA-256. |

### Digests

Both digests hash UTF-8 JSON lines of `[start_ms, end_ms, text]`, one per line, each followed by `\n`, with no spaces (`separators=(",", ":")`) and non-ASCII characters kept as-is.

- `transcript.revision_sha256` covers every segment of the selected transcript in `start_ms` order, with absolute times. It identifies the transcript revision; it changes when any segment's text or timing changes.
- `captions.text_sha256` covers the cues exactly as delivered, with clip-relative times, so a consumer can recompute it from the document.

### Cue timing and bounds

Timing is segment-level; word timings are not included. A segment that overlaps the selection is clipped to it. A segment with no usable end (its end is not after its start) is included only if it starts inside the selection, with `end_ms: null` rather than an invented end. At most 200 cues are included (`cues_truncated: true` past that), and cue text is cut at 2,000 characters with `text_truncated: true`.

## What the handoff is not

The provenance block is a declaration of what was selected. It is not proof of rights to use the media. For registered originals the operator's rights note stays private; the handoff carries only the original's hash and the timeline-alignment confirmation. The document never contains account IDs, filesystem paths, session credentials or the authenticated download URL.

## Failures

The handoff follows the download's rules. It returns 404 when exports are disabled, when the caller does not own the export, when the export is still queued or running, has failed, expired or been revoked, when the source recording was deleted, and when the stored MP4 no longer matches its recorded hash. Exports requested before this feature have no snapshot and return 409; request the range again.

## Verification

`tests/test_clip_handoff.py` covers cue clipping and relative timing, untimed segments, both digests, revision changes, the cue and text bounds, request-time snapshots surviving a transcript edit, a fresh export after that edit, an unavailable selected source, the original-file handoff without its rights note, and every failure case above. The fixture test checks that the committed example validates and that its cue digest recomputes.
