# Anonymous search admission

Anonymous full-text search, grouped search, mention maps and both search exports share two PostgreSQL session advisory-lock slots. ANONYMOUS_SEARCH_CONCURRENCY configures 1–16 slots and must be identical across API workers. Excess requests return HTTP 429 with Retry-After: 10 and private, no-store. A database-validated session exempts authenticated users. Suggestions, history, health and other routes do not consume slots.

A separate checked-out connection holds each lock until the request finishes, even when analytics commits the request transaction. Cleanup unlocks explicitly; failed cleanup invalidates the connection so a held lock cannot return to the pool. Process death closes connections and releases locks. No schema migration is required.

Mention maps cache public results in Redis for 60 seconds using all search parameters in a SHA-256 key. Authentication and analytics run on every request. Redis failure falls back to a bounded uncached search. This reduces repeated queries but does not eliminate first visits to different topics.
