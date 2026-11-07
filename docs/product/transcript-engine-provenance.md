# Transcript engine provenance

The worker does not always run the model named in `WHISPER_MODEL`. With
`FORCE_GPU=true` it walks `GPU_MODEL_FALLBACKS` until a model loads, and on
ROCm a crashed PyTorch chunk is redone with a CTranslate2 model that can step
down to `medium`. Each native transcript now records the engine that actually
produced it.

## What is stored

`worker/whisper_runner.py` reports, for every chunk, the model, backend
(`faster-whisper` or `whisper`), device, compute type and installed library
version that ran. The native pipeline stores them on the `transcripts` row:

| Column | Example |
|---|---|
| `engine_model` | `medium` after a fallback from `large-v3` |
| `engine_backend` | `faster-whisper` |
| `engine_device` | `cuda`, `cpu`, or the requested device when the library does not say |
| `engine_compute_type` | `float16` |
| `engine_version` | `1.1.1`, read from the installed package |

`transcripts.model` now holds the model that ran as well. If any chunk of a
video fell back to the ROCm CTranslate2 model, the whole transcript records
that fallback engine, so a mixed transcript is never reported as the primary
engine.

Transcripts stored before migration `20261107_transcript_engine` keep NULL in
these columns. Nothing is backfilled, because the old `model` value was the
configured model and may not be the one that ran.

The per-job quality preset (for example `fast`, whose preset model is `base`)
does not change the loaded model. The worker loads one model per process, so
the recorded engine is that model whatever preset the job asked for.

## Reading it

`GET /videos/{video_id}/transcript/metadata` returns the language, the stored
`model`, `provenance` and `engine`. `provenance` is `recorded` when the engine
was stored at transcription time and `unknown` for older rows, whose `engine`
is null. The endpoint returns 404 for an unknown video and 409
`transcript_not_ready` when the video has no native transcript. Like the
transcript itself, it needs no sign-in.

## Upgrade requeue

The worker can redo completed native transcripts that did not come from a
target engine. It is off unless `MODEL_UPGRADE_TARGET_MODEL` is set:

| Setting | Meaning |
|---|---|
| `MODEL_UPGRADE_TARGET_MODEL` | Model every transcript should come from. Empty disables the requeue. |
| `MODEL_UPGRADE_TARGET_BACKEND` | Optional; also redo transcripts from another backend. |
| `MODEL_UPGRADE_TARGET_VERSION` | Optional; also redo transcripts from another library version. |
| `MODEL_UPGRADE_BATCH_SIZE` | Videos requeued per worker loop, oldest transcript first (default 5). |

Model names are compared as text, so new names such as distilled or turbo
variants work without code changes. The target is not a ranking: a transcript
from any other model is redone, including a larger one. Rows without recorded
provenance are compared by their stored `model`. When a backend or version is
set, their unknown engine counts as a mismatch and they are redone.

Two guards stop the requeue from redoing the same work in a loop:

- A worker requeues only after it has loaded an engine that matches the
  target. A worker that fell back to a smaller model leaves the backlog alone.
  The model loads on the first native transcription, so an idle worker does not
  start the requeue until it has transcribed something.
- Only transcripts created before that worker loaded its model are eligible.
  A transcript it just produced with the ROCm chunk fallback is not requeued
  again until the worker restarts.

This replaces the earlier requeue, which ranked `tiny` through `large-v3`
against `WHISPER_MODEL` on every loop, without a limit. That requeue no longer
runs; set `MODEL_UPGRADE_TARGET_MODEL` to get the behavior back.
