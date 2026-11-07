# Channel sync inclusion rules

**Status:** shipped (2026-11-07).

Recurring channel sync lists each configured channel's uploads every
`CHANNEL_SYNC_INTERVAL_SECONDS` and enqueues the ones not yet in the archive.
A VOD channel can also carry shorts, trailers, clips and re-uploads, and each
one costs caption ingest and GPU time. Inclusion rules let a deployment say
which uploads a channel should contribute.

Rules apply only to recurring sync. Channel jobs created through `POST /jobs`
are unaffected. A rule never removes or changes a video that is already in the
archive; it only decides whether a newly listed upload is enqueued.

## Configuration

`CHANNEL_SYNC_URLS` keeps working unchanged: each comma-separated URL is a
channel with no rules. To add rules, set `CHANNEL_SYNC_CONFIG` to the path of a
JSON file inside the worker container. Both can be used together, but a channel
URL may appear only once across them.

```json
{
  "channels": [
    {
      "url": "https://www.youtube.com/@HasanAbiVODs/videos",
      "min_duration_seconds": 1800,
      "missing_duration": "include",
      "title_exclude": ["(?i)#shorts", "(?i)\\btrailer\\b"],
      "published_after": "2024-01-01",
      "missing_published_date": "include"
    },
    { "url": "https://www.youtube.com/@HasanAbiVODsBackup/videos" }
  ]
}
```

| Key | Meaning |
|---|---|
| `url` | Required. The channel URL, as it would appear in `CHANNEL_SYNC_URLS`. |
| `min_duration_seconds` | Skip uploads shorter than this. |
| `max_duration_seconds` | Skip uploads longer than this. Must not be below `min_duration_seconds`. |
| `missing_duration` | `"include"` or `"skip"`. Required when either duration bound is set; decides uploads the listing gives no duration for (premieres and live streams often lack one). |
| `title_include` | 1–20 regular expressions. When set, an upload is skipped unless its title matches at least one. |
| `title_exclude` | 1–20 regular expressions. An upload whose title matches any of them is skipped. |
| `published_after` | `YYYY-MM-DD`. Skip uploads published before this UTC day; uploads on that day are kept. |
| `missing_published_date` | `"include"` or `"skip"`. Required with `published_after`; decides uploads the listing gives no date for. |

Patterns use Python `re` syntax and are matched with `search` against the first
300 characters of the title. Matching is case-sensitive; start a pattern with
`(?i)` to ignore case. Each pattern is at most 200 characters. In JSON, a
backslash must be doubled (`"\\b"`).

Flat channel listings from yt-dlp usually carry no publication date, so with
`published_after` the `missing_published_date` choice decides most uploads.
The worker reads `release_timestamp`, `timestamp` or `upload_date` when the
listing has them.

The file is at most 64 KiB and lists at most 100 channels, counting
`CHANNEL_SYNC_URLS`.

## Validation

The settings loader parses the file when the worker starts, so a bad file
stops startup with a message naming the entry and key, for example
`CHANNEL_SYNC_CONFIG channels[0]: title_exclude[1] is not a valid regular expression (missing ), unterminated subpattern)`.
Startup fails on invalid JSON, unknown keys, a regex that does not compile, a
minimum duration above the maximum, a negative duration, a malformed date, a
missing `missing_duration` or `missing_published_date` choice, a choice set
without its rule, and a channel listed twice.

The release preflight (`scripts/compose_prod.sh preflight`) runs the same
check. It finds the file through the worker's bind mounts in the rendered
Compose configuration and fails if `CHANNEL_SYNC_CONFIG` is not on one.

## Checking the rules

Rules run in this order and the first one that excludes an upload is counted:
duration bounds (or `missing_duration`), `title_include`, `title_exclude`,
`published_after` (or `missing_published_date`). After each channel with
skipped uploads the worker logs `Recurring channel uploads skipped by rule`
with `channel_url` and `skipped_by_rule`, a count per rule name. The summary
log `Recurring channel sync complete` includes the total `skipped`.

## Adopting it in a client deployment

Only deployment files change. Put the rules file in the deployment directory,
for example `channel-sync.json`, and add to `docker-compose.client.yml`:

```yaml
services:
    worker:
        environment:
            CHANNEL_SYNC_CONFIG: /app/config/channel-sync/channel-sync.json
        volumes:
            - ./channel-sync.json:/app/config/channel-sync/channel-sync.json:ro
```

Set `CHANNEL_SYNC_CONFIG` on the worker service rather than in the shared
operator env file: every service validates it at startup, and only the worker
mounts the file. Remove a channel from `CHANNEL_SYNC_URLS` when you move it
into the file. Run the preflight before deploying.
