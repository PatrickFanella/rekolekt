# Broadcast dates and day coverage

VOD channels upload a broadcast days after it happened, and archive channels
re-upload old broadcasts years later. `videos.uploaded_at` records the YouTube
upload, so it is the wrong answer to "when did this happen?". Periods,
timelines, topic mentions, feeds and search use `videos.broadcast_date`
instead.

## How a video gets its date

`app/archive/broadcast_dates.py` derives three columns for every video when it
is inserted, and again on each archive intelligence refresh:

| Column | Values |
|---|---|
| `broadcast_date` | The date written in the title, or the UTC upload day when the title has none. NULL when neither exists. |
| `broadcast_date_source` | `title`, `upload_date` (an approximation) or `manual`. The refresh never overwrites `manual` rows. |
| `broadcast_kind` | `vod` (a full broadcast), `no_stream` (a placeholder upload for a day off) or `extra` (clips, interviews, podcasts). |

Title dates accepted: `November 25, 2024`, `Nov. 25th 2024`, `July 17 & 18, 2022`
(the first day), `2025-03-26`, `2023/12/31` and numeric `14/09/2025` or `26/11/24`.
A title date later than the upload (allowing two days) or in the future is
ignored.

`ARCHIVE_EDITORIAL_PRESET` selects two conventions:

- `hasanara`: numeric dates are day-first, and only titles with a spelled-out
  date are full broadcasts. Clips and media appearances become `extra`.
- `generic`: numeric dates are month-first, and every video is a `vod` unless
  its title says "no stream" or "clip".

A numeric date with a day above 12 is read unambiguously under either preset.

To correct a date by hand, set `broadcast_date`, `broadcast_kind` and
`broadcast_date_source = 'manual'` on the row.

## Day coverage

Named periods, period summaries and timeline buckets report `coverage`:

- `days`: days in the period from the first archived day up to today.
- `vod_days`: days with a transcribed full broadcast.
- `no_stream_days`: days with only a "no stream" placeholder.
- `unknown_days`: the rest, meaning nothing archived for that day.
- `missing_dates`: the first 62 unknown days.

A period entirely before the archive or in the future has no coverage
(`null`) rather than 0 %. "No stream" placeholders count toward coverage but
never toward video counts.

## Deploying

Migration `20261105_broadcast_dates` adds the columns, backfills every video,
and repoints `archive_topic_mentions.occurred_at` at the broadcast date. Named
period coverage is filled by the next archive intelligence refresh.
