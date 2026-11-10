# Rekolekt

![Rekolekt: The sentence, and where it was said. Green index mark on a dark archive field.](docs/assets/readme/banner.png)

Rekolekt is software for running an archive of long recordings. It transcribes
each recording with timestamps, indexes the text, and links every passage back
to the second it was spoken in the source video.

You deploy it yourself; it is not a hosted upload service. It suits people who
maintain a collection and want others to search and cite it: a creator with a
back catalog, a research group, a community keeping its own reference.
[HasanAra](https://hasanara.tv) is one archive running on it.

This repository is the headless core: the backend services and the HTTP API.
It ships no web pages. Each archive builds its own frontend against the API
contract in [`docs/api/openapi.json`](docs/api/openapi.json). The
[`rekolekt-web`](https://github.com/subculture-collective/rekolekt-web) kit
provides an API client, generated types, hooks and default components for that.

Product page: [rekolekt.subcult.tv](https://rekolekt.subcult.tv)

## Features

- Ingestion from YouTube videos and channels with yt-dlp, as durable jobs with
  retry, cancel and quotas. Recurring channel sync can filter uploads per channel
  by duration, title and date ([rules](docs/product/channel-sync-rules.md)).
- Whisper transcription on CPU, CUDA or ROCm, with YouTube captions imported
  alongside when available. Each transcript records the engine that produced it.
- Optional speaker diarization, with diarized speakers matched to named people
  in the archive and search filtered by speaker.
- Full-text search in PostgreSQL, with optional OpenSearch acceleration.
- Passage links that resolve a start and end into a shareable range with its
  source context, plus share cards. Basic passage links need no account
  ([passage sharing](docs/product/passage-sharing.md)).
- Transcript exports as JSON, SRT, VTT and PDF, and stored word timings
  ([word timings](docs/product/word-timings.md)).
- Accounts through Google or Twitch OAuth, with linked sign-ins, sessions, saved
  searches, saved moments and scoped API keys.
- Reviewed transcript corrections from signed-in users
  ([corrections](docs/product/transcript-corrections.md)).
- Archive browsing by broadcast date, topics, periods and timelines, with
  optional LLM topic and chapter enrichment.
- Optional modules, each switched on by a server setting: clip exports
  (`CLIP_EXPORTS_ENABLED`), chat replay (`CHAT_REPLAY_ENABLED`), a Podcasting 2.0
  chapters file and recordings RSS feed (`PODCAST_FEED_ENABLED`), a creator
  community (`COMMUNITY_ENABLED`) and an AT Protocol account bridge
  (`ATPROTO_ENABLED`).
- A per-archive site profile (name, description, notices, artwork URLs) served
  at `/site`.
- Admin endpoints for jobs, search status, moderation and editorial review,
  plus Prometheus metrics and health checks.

Transcripts are machine-made and can contain errors; check the recording before
quoting. Passage links point to the currently available source and transcript;
they are not immutable citations or downloaded video clips.

## Quick start

Local development needs Python 3.11 and Docker with Compose.

```bash
git clone https://github.com/subculture-collective/rekolekt.git
cd rekolekt
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
ALLOW_SESSION_TOKEN_CONTRACT_MIGRATION=true docker compose up --build db redis migrations api
```

The API listens on `http://localhost:8000`, with interactive documentation at
`/docs`. The migration opt-in is for a fresh database only. `make gen-ports`
writes a `.env` with random free host ports if the defaults are taken.

Run the full check before opening a pull request:

```bash
PYTHON_BIN="$PWD/.venv/bin/python" make verify
```

`make verify` starts disposable PostgreSQL, Redis and OpenSearch services,
applies every migration, runs linting, the mypy baseline and the backend tests
with coverage, validates the docs, audits Python dependencies, and fails if
`docs/api/openapi.json` is out of date. After an API change, run `make openapi`
and commit the regenerated file. The [testing guide](docs/development/testing.md)
lists focused commands.

## Configuration

All settings are environment variables; [`.env.example`](.env.example) lists
them with comments. The main groups:

- Database and cache: `DATABASE_URL`, `REDIS_URL`, `SEARCH_BACKEND`,
  `OPENSEARCH_URL`.
- Web and sessions: `FRONTEND_ORIGIN`, `CORS_ALLOW_ORIGINS`, `SESSION_SECRET`,
  `ANALYTICS_HMAC_SECRET`, `BOOTSTRAP_ADMIN_IDENTITIES`.
- OAuth: `OAUTH_GOOGLE_CLIENT_ID`, `OAUTH_GOOGLE_REDIRECT_URI`,
  `OAUTH_TWITCH_CLIENT_ID`, `OAUTH_TWITCH_REDIRECT_URI` and their secrets.
- Transcription: `WHISPER_MODEL`, `WHISPER_BACKEND`, `WHISPER_LANGUAGE`,
  `CHUNK_SECONDS`, `MAX_PARALLEL_JOBS`, `FORCE_GPU`, `ROCM`.
- Diarization: `ENABLE_DIARIZATION`, `DIARIZATION_MODEL`, `HF_TOKEN`.
- YouTube download: the `YTDLP_*` and `PO_TOKEN_*` settings
  ([ingestion setup](docs/youtube-ingestion-setup.md)).
- Ingestion: `CHANNEL_SYNC_URLS`, `CHANNEL_SYNC_CONFIG`, `JOB_CREATE_*` quotas.
- Enrichment: `ARCHIVE_ENRICHMENT_*`.
- Site identity: `SITE_PROFILE_PATH`, `SITE_NAME`, `SITE_DESCRIPTION`. Start
  from [`config/branding/northstar.json`](config/branding/northstar.json) and
  check it against [`config/branding/schema.json`](config/branding/schema.json).
- Backups: `BACKUP_*`, `WALG_*` and the object storage credentials.

## Deploy

Releases publish digest-pinned backend images for each role (`api`,
`ingest-cuda`, `ml-cuda`, `postgres-walg`, plus a pinned `redis`) with a
`release-images.json` manifest. An archive runs those images with
`docker-compose.production.yml` and `docker-compose.release.yml`, adds its own
overlay for domains, branding and channel sources, and hosts its frontend
separately with `FRONTEND_ORIGIN` pointing at it.
[Client branding and deployment](docs/deployment/client-branding.md) describes
the layout and a planner that turns a release manifest and a client inventory
into Compose overrides.

Kubernetes manifests (`k8s/`), a Helm chart (`charts/transcript-create`),
Ansible roles (`ansible/`) and Terraform for AWS (`terraform/aws`) are also in
the repository. The [deployment matrix](docs/deployment/README.md) gives the
order of operations for an upgrade, and [migrations](docs/MIGRATIONS.md)
covers the database.

## Project layout

| Path | Contents |
| --- | --- |
| `app/` | FastAPI routes, services, persistence and schemas |
| `worker/` | Ingestion, transcription, diarization and background jobs |
| `alembic/` | Database migrations |
| `clients/` | JavaScript and Python API client libraries |
| `config/` | Branding profiles and schema, monitoring and database config |
| `landing/` | Static product page |
| `scripts/` | Verification, backup, backfill and operator tools |
| `docs/` | API reference, architecture, deployment and product guides |

## Contributing

Report bugs and request features in
[GitHub issues](https://github.com/subculture-collective/rekolekt/issues). Ask
questions and share ideas in
[GitHub Discussions](https://github.com/subculture-collective/rekolekt/discussions).

Send code as a pull request on GitHub against `main`. CI runs `make verify` on
every pull request and it must pass before merge. Read
[CONTRIBUTING.md](CONTRIBUTING.md), the [Code of Conduct](CODE_OF_CONDUCT.md)
and the [security policy](docs/SECURITY.md). Commit messages follow
Conventional Commits.

The `/api` routes are v1-stable: changes are additive, deprecations stay for at
least two releases, and breaking changes go to `/api/v2`. See the
[API reference](docs/api-reference.md) and the [documentation index](docs/STATUS.md).

Rekolekt was previously named transcript-create, and some package names and
deployment identifiers still use that name. Built by [SUBCULT](https://subcult.tv).

## License

[Apache License 2.0](LICENSE), with [third-party notices](docs/THIRD_PARTY_NOTICES.md).
