# YouTube clip exports

**Status:** implemented, opt-in. Local checks do not enable production exports.

Select a transcript segment and choose **Start clip here**, then select the final
segment and choose **End clip here**. The passage editor also accepts exact times.
**Export and download** queues an MP4 and starts its download automatically when
it is ready. Keep the page open until the download starts. A manual download link
remains available if the browser blocks automatic downloads.

**Save range** stores the video and both millisecond boundaries in the account's
existing favorites table. Saved moments reopen the whole passage, including its
end boundary, so the same clip can be generated later. Saving a range does not
acquire media. Exporting a range downloads media through yt-dlp.

## Lifetime

`CLIP_CACHE_TTL_SECONDS` defaults to 3600 (one hour), measured from successful
render completion. It accepts 60–86400 seconds. Downloads become unavailable
at expiry even if cleanup has not run. The clip worker removes expired job
records, MP4s and acquisition scratch files automatically. It checks for work
and cleanup every five seconds when idle; a running job can delay physical
cleanup by its download/render timeout. The original YouTube recording is
never retained in the spool: yt-dlp requests only the selected time section,
and intermediate section files are removed after rendering or failure.

Favorites remain in PostgreSQL until the user deletes them or their account or
source video is deleted. They are independent of the disposable spool. An
identical, unexpired request reuses its queued/running/completed job. Exporting
an expired selection creates another job and download. Regeneration needs the
YouTube source to remain available.

## API

All clip routes require an authenticated account and an enabled feature.

- `POST /clips`: supply `video_id`, `start_ms`, `end_ms`. The video must already
  exist in the archive. The API reads its YouTube ID from PostgreSQL; arbitrary
  source URLs and downloader arguments are not accepted.
- `POST /users/me/favorites`: save `video_id`, `start_ms`, `end_ms` and optional
  transcript text using the existing favorites API.
- `POST /clips/from-favorite/{favorite_id}`: export the authenticated account's
  saved range, reusing a cached job or regenerating after expiry.
- `GET /clips/{job_id}`: check queued/running/completed/failed/expired status.
- `GET /clips/{job_id}/download`: private MP4 attachment, `Cache-Control:
  private, no-store`. Owners alone can read/download their exports. Source
  deletion blocks subsequent access.

The frontend polls pending jobs automatically and requests the download once
per export action. Closing the passage panel cancels its polling; it does not
cancel the server job. Expiry, failure and network problems are shown in the
panel. Background tabs and browser download permissions can delay or block the
browser action; the server cannot write directly to a device's Downloads folder.

## Running the worker

Set `CLIP_EXPORTS_ENABLED=true`, `CLIP_EXPORT_SOURCE=youtube`, and
`CLIP_SPOOL_DIR` for the API. The worker needs the same spool and cache lifetime,
Python 3.11 with the pinned yt-dlp, ffmpeg/ffprobe, and a supported JavaScript
runtime. The API only queues jobs and serves results; downloads and rendering
run in the separate worker. Run it with:

```bash
CLIP_EXPORT_SOURCE=youtube CLIP_SPOOL_DIR=/srv/clip-spool \
  python scripts/clip_exports.py work
```

The spool is private (mode 700), files are mode 600. Run API and worker with the
same Unix UID. `Dockerfile.clips` and the optional `docker-compose.clips.yml`
overlay provide a dedicated worker, a shared spool volume, matching UID 10001,
CPU/memory/process limits and automatic restart. The overlay changes the API's
UID: check permissions on existing API mounts before adopting it. It is a
local Compose overlay, not a digest-pinned production release manifest.

`YTDLP_COOKIES_PATH` can point to an operator-mounted cookies file when needed;
never expose that file to the API caller. Configure it in the worker environment
and mount it read-only. `JS_RUNTIME_CMD` selects the runtime (the clip image uses
Node). Existing local-original exports remain supported through
`CLIP_EXPORT_SOURCE=original` and `CLIP_ORIGINALS_ROOT`.

## Bounds and checks

Selections must be positive, ordered and at most two minutes. Known source
lengths bound the end timestamp. Output is H.264/AAC MP4, scaled within 1280×720,
at most 100 MiB, with duration verified to within 250 ms. There is one renderer
per spool, a twenty-job active cache/queue limit, a ten-minute yt-dlp timeout,
a three-minute render timeout, and a 20 GiB free-space reserve. A timed-out
acquisition terminates its process group, including ffmpeg. Hard-killed jobs
expire into cleanup; they are not silently retried.

Tests cover real ffmpeg output, yt-dlp command construction, ownership, source
deletion, timeout cleanup, cache reuse, expiry and regeneration from an unchanged
favorite. Frontend tests cover automatic polling and download, cached downloads,
retry and cancellation, saving ranges and restoring their end boundary. A mocked
acquisition does not prove that a live YouTube source is accessible from a
particular deployment; cookies, network restrictions and provider changes still
apply.

Local verification on November 5, 2026: the backend suite passed 1,936 tests
with 83.79% coverage. A real yt-dlp acquisition from `jNQXAC9IVRw` produced a
three-second, 359,208-byte MP4. Chromium automatically downloaded those exact
bytes after two status checks, using mocked account/job responses and an actual
HTTP attachment. The worker image built and its unprivileged, network-disabled
idle invocation returned `idle`. These checks are local, not a production rollout.
