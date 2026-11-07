# Word timings

**Status:** shipped API contract (2026-11-07).

When `WHISPER_WORD_TIMESTAMPS` or a job's quality preset turns word timestamps on, the
native pipeline stores each segment's Whisper word list in `segment_words`. Frontends can
ask for those timings to highlight the spoken word or snap a passage to a word instead of
a whole segment.

## Requesting words

Add `include=words` to either endpoint:

- `GET /videos/{video_id}/transcript` in `raw` or `formatted` mode. `mode=cleaned`
  returns 422 because cleaned text no longer matches the stored words.
- `GET /videos/{video_id}/transcript.json` (authentication still required).

Without the parameter, both responses are unchanged: segments carry `start_ms`, `end_ms`,
`text` and `speaker_label`, and no word fields appear.

With the parameter, each segment gains `words`, and the response gains `word_timings`
and `word_timings_note`:

```json
{
  "segments": [
    {
      "start_ms": 1000,
      "end_ms": 3000,
      "text": "Hello there.",
      "speaker_label": "Speaker 1",
      "words": [
        {"start_ms": 1000, "end_ms": 1500, "text": "Hello", "probability": 0.97},
        {"start_ms": 1500, "end_ms": 3000, "text": "there.", "probability": 0.9}
      ]
    }
  ],
  "word_timings": "available",
  "word_timings_note": null
}
```

`probability` is null when the backend did not store one. Words keep their stored order.

## When there are no word timings

A segment with no usable timings has `words: null`; the API never invents times.
`word_timings` summarizes the response:

| `word_timings` | Meaning |
| --- | --- |
| `available` | Every segment has words. |
| `partial` | Some segments have `words: null`. |
| `unavailable` | No segment has words. |

`word_timings_note` gives the reason segments lack words:

| `word_timings_note` | Cause |
| --- | --- |
| `not_stored` | No stored list (the `fast` preset, word timestamps off, rows from before storage) or one that failed validation. |
| `word_limit_reached` | The response hit its word limit. |
| `youtube_captions` | The response comes from YouTube captions, which have no word timings. |
| `merged_transcript` | The response is a merged transcript; its segments do not map to stored Whisper segments. |

## Normalization

faster-whisper and openai-whisper both store `{"word", "start", "end", "probability"}` in
absolute seconds; openai-whisper rounds to 10 ms and may omit `probability`. Lists with
`text` instead of `word`, or `start_ms` and `end_ms` instead of seconds, are accepted too.
Seconds become milliseconds with the same truncation the worker uses for segment bounds.

A word is dropped when it has no text, text longer than 100 characters, missing or
non-numeric times, or an end before its start. It is also dropped when it lies more than
50 ms outside its segment. Words within 50 ms are clamped to the segment bounds, which
absorbs rounding drift. A segment returns `words: null` when the stored value is not a JSON
list, contains a non-object entry, holds more than 300 words, or has no word left after
these checks.

## Size limit

One response carries at most **100,000 words**: about nine hours of speech at 180 words
a minute, or roughly 8 MB of JSON before gzip. Once the limit would be exceeded, that
segment and every later one return `words: null`, and the note is `word_limit_reached`.
The constants live in `app/transcripts/words.py`.

## Caching and deletion

Word responses use their own ETag (the default ETag plus `-words`) and the same
`Cache-Control` policy as the default response. The default path keeps its Redis segment
cache. Word lists are read from Postgres on each request and are not cached in Redis.
Deleting a source deletes its segments, and `segment_words` rows go with them through
`ON DELETE CASCADE`.

WebVTT and SRT exports do not carry word timings. Forced alignment (for example WhisperX)
is not part of this contract.
