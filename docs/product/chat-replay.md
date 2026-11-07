# Chat replay

**Status:** shipped behind `CHAT_REPLAY_ENABLED` (default off), 2026-11-07.

An operator can attach a broadcast's chat log to a recording. The messages are
placed on the recording's timeline and served by time range, so a frontend can
show chat beside the transcript.

Archived recordings are often YouTube re-uploads of Twitch broadcasts. The chat
log is timed against the Twitch broadcast, so an import also needs an
alignment: the mapping from broadcast time to recording time. The alignment can
come from the audio of both files, or from a manual offset.

## What is stored

`chat_imports` holds one row per recording:

| Column | Meaning |
|---|---|
| `source_format` | `twitchdownloader` or `rustlog` |
| `source_name`, `source_digest` | File name and SHA-256 of the imported file |
| `broadcast_started_at` | The `--broadcast-start` given for a rustlog export |
| `alignment_method` | `audio` or `manual` |
| `alignment_mapping` | Segments `{source_start_ms, source_end_ms, recording_start_ms}`; a manual offset is one open-ended segment |
| `alignment_confidence` | 0 to 1 for `audio`; null for `manual` |
| `message_count`, `imported_at` | Stored messages and import time |

`chat_messages` holds each message's recording time (`offset_ms`), the author's
display name as it appears in the log, the text, and optional colour, badges
and emotes.

A message is dropped when its broadcast time falls outside every segment (for
example, in a section the re-upload cut) or after the recording ends. The
import summary counts these.

Chat is kept until the recording is deleted. Deleting the source recording
removes its import and all its messages. Importing again replaces the previous
import and its messages in one transaction; a failed import leaves the
previous one in place.

## Getting the files before the Twitch VOD expires

Twitch deletes past broadcasts after 7 to 60 days, depending on the channel.
Automatic alignment needs the broadcast's audio, so download the chat and the
audio while the VOD exists:

```bash
# Chat, timed from the start of the VOD
TwitchDownloaderCLI chatdownload --id 2000000000 -o chat-2000000000.json
# Audio only; a 6-hour stream is a few hundred MB
yt-dlp -f bestaudio -o 'twitch-2000000000.%(ext)s' https://www.twitch.tv/videos/2000000000
```

The recording's audio can be any file ffmpeg reads: the archive's own WAV, or
the YouTube upload fetched with `yt-dlp -f bestaudio`.

Without the broadcast audio, use a manual offset. rustlog keeps chat by
channel and date after the VOD is gone; export the broadcast's time range as
JSON and pass the broadcast start time.

## Importing

Run the CLI where the worker dependencies (numpy, ffmpeg) are installed and
`DATABASE_URL` points at the archive:

```bash
# Preview the audio alignment without writing anything
python scripts/import_chat.py align \
  --broadcast-audio twitch-2000000000.m4a --recording-audio recording.wav

# Import with automatic alignment
python scripts/import_chat.py import --video-id <uuid> \
  --chat chat-2000000000.json --format twitchdownloader \
  --broadcast-audio twitch-2000000000.m4a --recording-audio recording.wav

# Import a rustlog export with a manual offset
python scripts/import_chat.py import --video-id <uuid> \
  --chat rustlog.json --format rustlog --broadcast-start 2026-11-07T18:02:11Z \
  --offset-ms -37040
```

`--offset-ms` is recording time minus broadcast time. It is negative when the
re-upload starts after the broadcast began. `--dry-run` validates, aligns and
reports, then rolls back.

Input limits: the file must be at most 256 MiB (`--max-bytes` changes this),
hold at most 2,000,000 entries, and every message needs a 1–64 character
author and 1–1000 characters of text. Any other problem rejects the whole file
with the message index and reason. The CLI exits with status 2 on rejection.

## Automatic alignment

`app/chat_alignment.py` decodes both files with ffmpeg to 8 kHz mono, in
one-minute chunks, and reduces them to a loudness envelope of 50 frames per
second (20 ms each). A 6-hour stream is about a million values, so memory stays
around 200 MB and a 6-hour pair aligns in about 20 seconds.

Each minute of the recording is cross-correlated against the whole broadcast
envelope. Consecutive minutes that match the same lag form a segment. Where
the lag changes (a cut), the boundary is placed at the frame that best
separates the two lags. Audio longer than 12 hours is refused.

Confidence is the share of recording minutes that matched, weighted by the
correlation of each match; silent minutes are left out. Below 0.6 (or
`--min-confidence`), the import is refused and asks for `--offset-ms`. Two
limits to know:

- A section shorter than about a minute that was cut or inserted may be
  missed.
- Content in the recording that is not in the broadcast (an added intro, for
  example) gets no mapping, which lowers the confidence.

## Serving chat

`GET /videos/{video_id}/chat?start_ms=&end_ms=&limit=` returns messages with
`start_ms <= offset_ms < end_ms`, ordered by time, at most `limit` (default
200, maximum 1000). When more messages exist, the response sets `truncated`
and `next_start_ms`; requesting again from `next_start_ms` neither repeats nor
skips messages. A recording without an import returns `imported: false` and no
messages. The endpoint is public like other video reads, and returns 404 while
`CHAT_REPLAY_ENABLED` is off. `GET /site` reports the flag as
`chat_replay_enabled`.
