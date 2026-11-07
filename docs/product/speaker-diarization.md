# Speaker diarization coverage

Diarization labels transcript segments with anonymous speakers (`Speaker 1`,
`Speaker 2`, ...). The labels are per recording: `Speaker 1` in one video is
not the same person as `Speaker 1` in another. The standalone worker
(`worker/diarization_loop.py`) runs pyannote `speaker-diarization-community-1`
and writes `segments.speaker_label`.

On 2026-11-07 HasanAra had labels for 97 of 3,088 videos. Two limits caused
most of the gap: the median broadcast runs about six hours, past the 2-hour
`DIARIZATION_MAX_DURATION_SECONDS`, and 1,677 diarization jobs failed because
the transcription worker had already deleted the WAV.

## Long recordings

The worker reads and diarizes a recording in windows of
`DIARIZATION_CHUNK_SECONDS` (default 600, the window size verified under the
production 4 GB memory limit). Only one window of audio is in memory at a time,
so memory depends on the window length, not the recording length. `0` diarizes
the whole file in one call, as before.

Each window gets its own pyannote labels. pyannote also returns one embedding
per speaker in the window. The worker links a window's speakers to speakers
already heard in the recording when their cosine similarity reaches
`DIARIZATION_LINK_THRESHOLD` (default 0.5). Matching is one-to-one within a
window, best similarity first, so two voices in one window never merge. A
speaker below the threshold, or without a usable embedding, gets a new label.
Labels are numbered by first appearance across the whole recording.

If a chunked recording returns no embeddings at all (for example the legacy
fallback model), the job fails instead of writing labels that disagree between
windows.

`DIARIZATION_MAX_DURATION_SECONDS` still applies. Raise it explicitly to
diarize long broadcasts.

## Missing audio

With `DIARIZATION_REFETCH_AUDIO=true`, a claimed video whose WAV is missing (or
was never stored) is downloaded again through the ingestion path
(`worker.audio.download_audio` and `ensure_wav_16k`) into
`DIARIZATION_REFETCH_DIR/<video-id>`. The directory is deleted when the job
ends, whether it succeeded or failed. A six-hour broadcast needs roughly 1 GB
of scratch space while it runs. Without the setting, a missing WAV still fails
the job with `WAV file missing for diarization`.

## Stored speaker embeddings

After writing labels, the worker replaces the recording's rows in
`video_speaker_centroids`: one unit-length embedding per label, the label's
speaking time, and the model that produced it. The embedding is the
speech-weighted mean of that speaker's window embeddings. Embeddings from
different models are not comparable, so consumers must match on `model`.
[Speaker identification](speaker-identification.md) compares them with
enrolled voices.

## Backlog runs

Normal runs still need `DIARIZATION_ALLOWED_VIDEO_IDS` (at most five UUIDs),
and the canary keeps its 600-second cap and row fencing. To widen coverage, an
operator runs a bounded backlog batch:

```bash
scripts/compose_prod.sh maintenance diarization-backlog <1..20> --approved
```

The helper runs the preflight and the diarization role check, then starts one
CPU worker with `DIARIZATION_BACKLOG_MODE=true`. At startup the worker selects
at most `DIARIZATION_BACKLOG_BATCH_SIZE` completed videos with transcript
segments, oldest first, that are pending or, with re-fetch on, failed only
because their WAV was missing. It returns those failed rows to pending and
uses the selection as its allow-list for the run, so every claim and write
keeps the existing allow-list guard. Canary rows, other failures and recordings
over the duration cap are never selected. The worker exits when the batch is
done. The helper sets a 12-hour duration cap, 600-second windows and re-fetch.

Backlog mode refuses to start with an explicit allow-list or in canary mode.

## Database role

The production `hasanara_diarization` role needs three grants beyond those
listed in the [GTX 1080 guide](../gtx-1080-core.md#production-diarization):

```sql
GRANT SELECT (youtube_id) ON videos TO hasanara_diarization;
GRANT SELECT (video_id), INSERT (video_id, speaker_label, embedding, speech_seconds, model)
    ON video_speaker_centroids TO hasanara_diarization;
GRANT DELETE ON video_speaker_centroids TO hasanara_diarization;
```

`scripts/check_diarization_role.sql` requires exactly these grants, so the
canary and backlog helpers refuse to run until they are applied.
