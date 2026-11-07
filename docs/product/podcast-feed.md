# Podcast feed and JSON chapters

**Status:** opt-in product contract (2026-11-07).

An archive can publish an RSS 2.0 feed of its completed recordings and serve each recording's
chapters in the [Podcasting 2.0](https://github.com/Podcastindex-org/podcast-namespace)
JSON chapters format. Feed readers can follow new recordings. A creator who runs their own
podcast feed can point `podcast:transcript` and `podcast:chapters` at the archive's URLs.

Both endpoints return 404 unless `PODCAST_FEED_ENABLED=true`. The default is off.

## JSON chapters

`GET /api/videos/{videoId}/podcast-chapters.json` returns a
[JSON chapters 1.2.0](https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/examples/chapters/jsonChapters.md)
document served as `application/json+chapters`. It lists only chapters with status
`published`, in ascending `startTime` order, with `startTime`, `endTime` and `title` in
seconds and text. The root carries `version`, `podcastName` (the site name) and the
recording `title`. Candidate, rejected and hidden chapters are never included. A recording
without published chapters returns 404, as does a missing or deleted recording.

## Recordings feed

`GET /api/feeds/recordings.rss` returns `application/rss+xml` and declares the
`podcast` namespace. It holds the 50 most recent recordings in state `completed`, newest
first by broadcast date and then ingestion time. Every recording the archive lists
publicly is eligible; the archive has no per-recording visibility setting. Each item has:

- `title`, `pubDate`, a `guid` with the recording ID, and a `link` to
  `FRONTEND_ORIGIN` + `/v/{videoId}`;
- two `podcast:transcript` entries with `rel="captions"`: `transcript.vtt` as `text/vtt`
  and `transcript.srt` as `application/x-subrip`, with a `language` attribute when the
  recording's language is known;
- `podcast:chapters` pointing at the JSON chapters document, only when published chapters
  exist.

All absolute URLs derive from `FRONTEND_ORIGIN`, with the API under `/api`. A misconfigured
origin returns 503. Text is escaped by the XML serializer, and characters that XML 1.0
cannot carry are removed.

The VTT and SRT exports still require a signed-in session, so anonymous podcast apps
cannot fetch the linked transcripts yet. The links resolve for signed-in readers.

## No enclosures

The feed emits no `enclosure`. Most archives, HasanAra included, do not hold distribution
rights for the audio. Podcast apps generally require an enclosure, so this feed suits feed
readers and creators who reference the transcript and chapter URLs from their own feed. An
enclosure option for deployments that own their audio is a possible follow-up.

## Deletion and caching

Source deletion removes the recording row, so it leaves the feed and its chapter and
transcript URLs return 404 on the next request. Both endpoints use the public read cache
policy (`max-age=300`, `stale-while-revalidate=60`), so shared caches can serve a deleted
recording for up to six minutes. Feed readers that already fetched an item keep their copy.

## Verification

`tests/test_podcast_feed.py` covers the disabled flag, validation of the chapters document
against a fixture schema transcribed from the JSON chapters spec, the published-only filter,
feed parsing, namespace, ordering, bounding, MIME types, escaping, the absent enclosure and
source deletion.
