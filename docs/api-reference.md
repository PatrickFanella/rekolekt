# API reference

**Status:** generated-contract companion (2026-07-12).

The machine-readable authority is [OpenAPI](api/openapi.json), a committed artifact. Rekolekt core is headless, so this contract is the product surface: archive frontends and the [`rekolekt-web`](https://git.subcult.tv/subculture-collective/rekolekt-web) kit generate their clients and types from it. `make verify` regenerates the document and fails on drift; `make openapi` writes it after an API change.

`GET /site` returns the public site profile: name, description, creator and operator names, tagline, notices, logo, favicon and social image URLs, and feature flags. Its `theme` object (`font`, `dark` and `light` colour tokens) is deprecated in the schema. It is still served so existing frontends work; new frontends should own their styling.

Primary groups are authentication, scoped API keys, search/grouped search/suggestions/mention exports, videos/transcripts/chapters/related/quoted moments, archive timelines/topics/opinions, favorites/saved searches, jobs/attempts, vocabularies, events, analytics reports, administration, exports, public support configuration, and health.

Access requirements are in [the access matrix](access-matrix.md). `GET /support` exposes only whether support is enabled and its validated public hosted link; the application has no billing, charge-creation, or card-data endpoint. `/api` changes are additive; deprecation response headers and release notes remain for two releases; breaking changes require `/api/v2`.

[Clip exports](product/youtube-clips.md) accept millisecond start/end ranges, use yt-dlp for YouTube sections when configured, and expose authenticated status/download endpoints. `POST /clips/from-favorite/{favorite_id}` regenerates a saved range after its cached MP4 expires. `GET /clips/{job_id}/handoff` returns the export's [provenance handoff](product/clip-handoff.md).

The opt-in [podcast feed](product/podcast-feed.md) (`PODCAST_FEED_ENABLED`) adds `GET /feeds/recordings.rss`, an RSS 2.0 feed of completed recordings with `podcast:transcript` and `podcast:chapters` links and no enclosures, and `GET /videos/{video_id}/podcast-chapters.json`, published chapters in the Podcasting 2.0 JSON chapters format.

`GET /videos/{video_id}/transcript` and `transcript.json` accept `include=words` to add stored Whisper [word timings](product/word-timings.md) to each segment, bounded to 100,000 words per response.

[Chat replay](product/chat-replay.md) serves imported chat for a recording by time range at `GET /videos/{video_id}/chat`. It returns 404 unless `CHAT_REPLAY_ENABLED` is set.
[Transcript corrections](product/transcript-corrections.md) are opt-in; their routes are registered only when `TRANSCRIPT_CORRECTIONS_ENABLED` is set, so the committed OpenAPI omits them. Transcript responses always carry a `corrections` list of corrected segment ranges with their machine text.
[Speaker identification](product/speaker-identification.md) adds `speaker=<person-slug>` to `/search`, `/search/grouped` and `/search/mention-map`, `speaker_person` to transcript segments and blocks, and admin endpoints under `/admin/videos/{video_id}/speakers` and `/admin/archive/metadata/people/{slug}/voice-references`.
