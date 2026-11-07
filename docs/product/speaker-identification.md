# Speaker identification

Diarization labels a recording's speakers `Speaker 1`, `Speaker 2` and so on,
and the labels mean nothing outside that recording (see
[speaker diarization coverage](speaker-diarization.md)). Speaker
identification attributes a recording's labels to archive people, so search
can answer "what did this guest say about X".

## Mappings

`video_speaker_people` maps `(video_id, speaker_label)` to an `archive_people`
row. Each mapping records its method (`manual` or `auto`), the match score for
automatic ones, the administrator who set it, and when. A mapping with no
person means an administrator cleared the label; automatic matching leaves
cleared labels alone.

Administrators manage mappings through these endpoints. Every write goes to the
audit log as an `admin_action` with an `operation` detail.

| Endpoint | Purpose |
| --- | --- |
| `GET /admin/videos/{video_id}/speakers` | Labels with segment count, speaking time, three sample segments (longest first), the current mapping and the best enrolled-voice suggestion |
| `PUT /admin/videos/{video_id}/speakers/{speaker_label}` | Map the label to `person_slug`; `enroll: true` also enrolls its voice |
| `DELETE /admin/videos/{video_id}/speakers/{speaker_label}` | Clear the label |

## Enrolled voices

`POST /admin/archive/metadata/people/{slug}/voice-references` takes a
recording, a label and the segments an administrator confirmed as that person.
All segments must carry the label. The stored reference is the label's voice
embedding from `video_speaker_centroids`, which pyannote computed over all of
that speaker's speech in the recording, so a label needs a stored embedding
before it can be enrolled. The marked segments are kept as evidence. `GET`
lists a person's references and `DELETE .../{reference_id}` removes one.
Enrolling a confirmed mapping with `enroll: true` uses the label's sample
segments as that evidence.

## Automatic matching

Each label's embedding is compared by cosine similarity with every enrolled
voice from the same diarization model. A person's score is their closest
reference; references taken from the recording being matched are ignored. The
best person is suggested in the admin listing at any score.

`scripts/match_speakers.py --limit N` reports the best match per unmapped
label. With `--apply` and `SPEAKER_AUTO_MATCH_ENABLED=true` it writes an
`auto` mapping when the score reaches `SPEAKER_MATCH_THRESHOLD` (default 0.75)
and leaves other labels unknown. It never replaces a current manual, automatic
or cleared mapping. The flag defaults to off.

Choose the threshold from evidence first. Label a sample by hand as a CSV with
`video_id,speaker_label,person_slug` (leave `person_slug` empty for a speaker
who is not an enrolled person), then run:

```bash
python scripts/evaluate_speaker_matching.py sample.csv --thresholds 0.5,0.6,0.7,0.8,0.9
```

For each threshold it prints how many labels would be attributed, how many
correctly, the precision, and the recall over labels whose true person is
enrolled. The script only reads the database.

## Search

`/search`, `/search/grouped` and `/search/mention-map` accept
`speaker=<person-slug>`. Results are limited to transcript segments whose label
currently maps to that published person. Captions have no speakers, so a
speaker filter returns no caption results.

The PostgreSQL path checks the mapping table directly. Search documents carry a
`speaker_person` keyword with the slug, which the OpenSearch path filters on.
The indexer adds the field to existing indexes at startup. Setting, clearing
or automatically writing a mapping queues the label's segments in the search
outbox and expires cached search results for the recording. A person's later
status change does not reindex their segments.

## Transcripts

Transcript segments and formatted blocks keep the raw `speaker_label` and add
`speaker_person` (slug and display name) when the label maps to a published
person.

## Reprocessing

When diarization changes any segment's label in a recording, or transcript
segments are deleted for re-transcription, the recording's mappings are marked
stale with the time and reason. Stale mappings stay visible in the admin
listing (`status: stale`) and no longer affect search or transcripts. A label
change also requeues the recording's segments in the search outbox. Mapping the
label again makes it current. Database triggers do this, so the restricted
diarization role needs no new privileges.
