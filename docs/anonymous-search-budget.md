# Anonymous search admission

Anonymous full-text search, grouped search, mention maps and both search exports share two PostgreSQL session advisory-lock slots. ANONYMOUS_SEARCH_CONCURRENCY configures 1–16 slots and must be identical across API workers. Excess requests return HTTP 429 with Retry-After: 10 and private, no-store. A database-validated session exempts authenticated users. Suggestions, history, health and other routes do not consume slots.

A separate checked-out connection holds each lock until the request finishes, even when analytics commits the request transaction. Cleanup unlocks explicitly; failed cleanup invalidates the connection so a held lock cannot return to the pool. Process death closes connections and releases locks. No schema migration is required.

Mention maps cache public results in Redis for 60 seconds using all search parameters in a SHA-256 key. Authentication and analytics run on every request. Redis failure falls back to a bounded uncached search. This reduces repeated queries but does not eliminate first visits to different topics.


## Attribution and query plans

Admission logs include the server request ID, route, anonymous/authenticated class, process ID, slot decision and release duration. They exclude query strings and cookies. Expensive repository statements carry the validated server UUID in a SQL comment, allowing a request to be matched to `pg_stat_activity` without logging its search text. Count client backends separately from parallel workers using `backend_type` and `leader_pid`; a parallel worker is not another admitted request.

Native transcript and video-title matches use disjoint, bounded branches. The text branch can use the full-text index; the title branch retains every matching title-only segment. A text-and-title match appears once. SQL NULL text-vector behavior, filters, sorting and pagination follow the original OR query.

Run `DATABASE_URL=<test database> python scripts/qualify_native_search.py` with the pinned Python environment for a reproducible plan comparison. It creates only session-local temporary relations, compares result IDs and emits three alternating EXPLAIN ANALYZE/BUFFERS samples for each query. The 100,000-row fixture is a query regression benchmark, not a production throughput estimate. Database connection values belong in the environment, not saved command logs.

`tests/test_search_budget_postgres.py` checks real advisory locks across processes, shared route saturation, recovery after endpoint errors/process death and exemption only for a database-validated session. The ordinary repository suite also compares native results with the original SQL across all sort orders and match modes, including NULL vectors and overlapping text/title matches.

On October 26, 2026, the fixture returned identical result IDs in three alternating comparisons. The original query took 33.8–34.1 ms and touched about 1,648 local-buffer blocks per run; the indexed branches took 0.38–0.45 ms and touched 28. These are PostgreSQL temporary-relation buffer counts, not physical-disk throughput measurements. A read-only candidate query against the live corpus completed the `solidarity` fixture in 162 ms; the original native query had reached the diagnostic five-second timeout. The common `people` fixture still reached that timeout and remains subject to admission and statement-timeout bounds.
