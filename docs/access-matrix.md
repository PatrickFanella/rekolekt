# Route, capability, and endpoint access matrix

**Status:** shipped contract (2026-07-12).

| Surface | Access |
| --- | --- |
| Home, search, Explore, episodes, timeline, topics, video, About, Support, Privacy, Terms | public |
| Donation configuration (`GET /support`) | public; returns only an allow-listed Stripe-hosted Payment Link or a disabled state |
| Saved and favorites | anonymous local-first; authenticated synchronization |
| Login | anonymous |
| Admin routes | authenticated plus `admin:access` capability |
| Search, public archive/video reads, suggestions, mention exports | public GET |
| Recordings RSS feed and JSON chapters | public GET; opt-in feature (`PODCAST_FEED_ENABLED`) |
| Favorites, saved searches, profile | authenticated owner |
| Clip request/status/download and export from a saved favorite | authenticated owner; opt-in feature; local-original mode also requires registered source ownership |
| Jobs list/read/write | authenticated owner or scoped API key (`jobs:read`, `jobs:write`) |
| Vocabulary mutation | authenticated owner; global management admin only |
| API key issuance | authenticated owner; admin scopes require audited admin issuance |
| Job requeue/quarantine/attempt administration and opinion correction | admin |
| Source deletion | owner or admin |
| Speaker label mapping and voice enrollment | admin |
| Chat replay (`GET /videos/{video_id}/chat`) | public GET; opt-in feature; chat is imported by an operator CLI, not the API |
| Transcript corrections (opt-in, `TRANSCRIPT_CORRECTIONS_ENABLED`) | propose and list own: authenticated; review queue, accept and reject: moderator; accepted corrections for a video: public |

API-key read scopes are `search:read`, `videos:read`, `exports:read`, and `jobs:read`; mutation scope is `jobs:write`.
