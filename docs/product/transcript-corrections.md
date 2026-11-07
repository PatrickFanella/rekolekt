# Transcript corrections

Local implementation, disabled by default (`TRANSCRIPT_CORRECTIONS_ENABLED=false`). Apply Alembic head before enabling. With the flag off, the correction routes are not registered and do not appear in the generated OpenAPI document. `GET /site` reports the flag as `transcript_corrections_enabled`.

Text only. Timings and speaker labels cannot be corrected.

## User journeys

- Member: select a segment → propose corrected text (1–2000 characters). The proposal names the video, the transcript source (`whisper` or `youtube`) and the segment's exact `start_ms`/`end_ms`. Guests get 401. A proposal identical to the current text is refused. Each account may submit ten proposals a minute; submissions serialize on the account row.
- Proposer: `GET /corrections/mine` lists the caller's own proposals with status and the moderator's reason.
- Moderator: `GET /corrections/queue` lists pending proposals oldest first, with `supporters`, the number of distinct accounts proposing the same text for the same segment. Proposer identity is not shown. Accept or reject with a reason (1–1000 characters). Acceptance remains a moderator decision regardless of support.
- Moderator: `GET /corrections/queue?status=stale` lists corrections a re-transcription could not re-apply, and pending proposals whose segment changed before review. Reject dismisses them; members can propose against the new text.
- Anyone: `GET /videos/{video_id}/corrections` lists the accepted corrections currently applied, with the machine text each replaced.

Every proposal, acceptance, rejection and stale outcome writes an audit record (`transcript_correction.*`) in the same transaction. Routes use the existing session account, CSRF protection and the community module's `moderator` role.

| Endpoint | Access |
| --- | --- |
| `POST /corrections` | signed in |
| `GET /corrections/mine` | signed in, own proposals |
| `GET /corrections/queue` | moderator or admin |
| `POST /corrections/{id}/accept`, `POST /corrections/{id}/reject` | moderator or admin |
| `GET /videos/{video_id}/corrections` | public |

## How accepted text is applied

A correction row in `segment_corrections` is keyed by video, source, segment time range and the SHA-256 of the machine text, not by segment row id. Acceptance writes the corrected text into the segment row and keeps the machine text on the correction row. Consequences:

- Transcript responses (`/videos/{id}/transcript` for the whisper and youtube sources, and `/videos/{id}/youtube-transcript`) return the corrected text and list each corrected range with its `machine_text` in `corrections`. Merged transcripts include corrected text but return an empty `corrections` list.
- Search reindexes through the existing segment outbox triggers. Exports, public passage pages and clip handoffs read the corrected segment text. Stored full text and the native formatted blocks are rebuilt on acceptance.
- Clip handoffs digest the transcript text, so a corrected transcript has a new `revision_sha256` and counts as a new selection.
- API caches for the video are invalidated after acceptance. HTTP caches (`max-age=3600`) and search result caches may serve the previous text until they expire, which the owner accepted on 2026-11-07.

Accepting a second correction for the same segment marks the first `superseded`. Acceptance checks the proposal's machine-text digest against the segment; if the segment changed since the proposal was made, the proposal becomes `stale` and the request returns 409.

## Re-transcription

Native transcription and caption re-ingest delete and rewrite segment rows. Both then call `reapply_corrections` in the same transaction. Each accepted correction is applied again where a segment with the same source and time range still carries the same machine text. Otherwise the correction becomes `stale` and appears in the moderator queue. A later rewrite that restores matching machine text re-applies a stale correction. No correction is applied to different text.

Whisper re-transcription usually changes segment boundaries, so expect most native corrections to go stale after a full re-run. Caption re-ingest of an unchanged track re-applies them.

## Deletion

- Source deletion removes the video and, by cascade, its corrections.
- Account deletion removes the account's pending, rejected and never-accepted proposals. Corrections that were accepted stay in the archive with the proposer cleared; a deleted reviewer is cleared the same way. Account merges carry proposals and reviews to the surviving account.

## Operating contract

Turning the flag off removes the routes but leaves accepted text in the transcripts. A migration downgrade restores the machine text for corrections still applied, then drops the table and its review history; take a backup first. Stored full text and native formatted blocks keep the corrected text after a downgrade until the next transcription or block backfill. This is review software, not a staffed moderation service.

## Verification

`tests/test_transcript_corrections.py` covers flag-gated registration, the authorization matrix, length and rate limits, auditing, proposer outcomes, transcript and search reads, outbox reindexing, clip handoff revisions, superseding, stale detection, re-application after re-transcription and caption re-ingest, account deletion and source deletion.
