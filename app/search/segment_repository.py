from __future__ import annotations

import json
import math
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.logging_config import request_id_ctx
from app.search.highlights import POSTGRES_HEADLINE_OPTIONS, normalize_search_rows


def _execute_search(db, sql: str, params: dict[str, Any]):
    # Correlate pg_stat_activity with admission logs without embedding search
    # text, cookies, or user identifiers. Only server-generated UUIDs are SQL.
    request_id = request_id_ctx.get()
    if request_id:
        try:
            correlation = str(UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            pass
        else:
            sql = f"/* hasanara.search request_id={correlation} */\n{sql}"
    return db.execute(text(sql), params)


def _match_expressions(
    alias: str, q: str, filters: dict[str, Any], params: dict[str, Any]
) -> tuple[str, str, str, str]:
    """Return parameterized text/title match, presentation, and rank SQL."""
    mode = filters.get("match_mode", "topic")
    text_column = f"{alias}.text"
    tsv_column = f"{alias}.text_tsv"
    if mode == "exact_phrase":
        simple_query = "phraseto_tsquery('simple', :q)"
        return (
            f"to_tsvector('simple', coalesce({text_column}, '')) @@ {simple_query}",
            f"ts_headline('simple', {text_column}, {simple_query})",
            f"ts_rank_cd(to_tsvector('simple', coalesce({text_column}, '')), {simple_query})",
            f"to_tsvector('simple', coalesce(v.title, '')) @@ {simple_query}",
        )
    if mode == "whole_word":
        simple_query = "plainto_tsquery('simple', :q)"
        return (
            f"to_tsvector('simple', coalesce({text_column}, '')) @@ {simple_query}",
            f"ts_headline('simple', {text_column}, {simple_query})",
            f"ts_rank_cd(to_tsvector('simple', coalesce({text_column}, '')), {simple_query})",
            f"to_tsvector('simple', coalesce(v.title, '')) @@ {simple_query}",
        )
    return (
        f"{tsv_column} @@ websearch_to_tsquery('english', :q)",
        f"ts_headline('english', {text_column}, websearch_to_tsquery('english', :q), :headline_options)",
        f"ts_rank_cd({tsv_column}, websearch_to_tsquery('english', :q))",
        "v.title ILIKE :title_q",
    )


def _video_filter_clauses(filters: dict[str, Any], params: dict[str, Any]) -> list[str]:
    """Return the video-level filters shared by every search branch."""
    clauses = []
    if filters.get("date_from"):
        clauses.append("v.broadcast_date >= CAST(:date_from AS date)")
        params["date_from"] = filters["date_from"]
    if filters.get("date_to"):
        clauses.append("v.broadcast_date <= CAST(:date_to AS date)")
        params["date_to"] = filters["date_to"]
    if filters.get("min_duration") is not None:
        clauses.append("v.duration_seconds >= :min_duration")
        params["min_duration"] = filters["min_duration"]
    if filters.get("max_duration") is not None:
        clauses.append("v.duration_seconds <= :max_duration")
        params["max_duration"] = filters["max_duration"]
    if filters.get("category"):
        clauses.append("v.category ILIKE :category")
        params["category"] = filters["category"]
    if filters.get("channel"):
        clauses.append("v.channel_name ILIKE :channel")
        params["channel"] = f"%{filters['channel']}%"
    if filters.get("language"):
        clauses.append("v.language = :language")
        params["language"] = filters["language"]
    return clauses


def _speaker_clause(filters: dict[str, Any], params: dict[str, Any], video_column: str = "s.video_id") -> str | None:
    """Native segments whose label is currently attributed to the published person."""
    if not filters.get("speaker"):
        return None
    params["speaker"] = filters["speaker"]
    return (
        "EXISTS (SELECT 1 FROM video_speaker_people sp JOIN archive_people spp ON spp.id = sp.person_id "
        f"WHERE sp.video_id = {video_column} AND sp.speaker_label = s.speaker_label "
        "AND sp.invalidated_at IS NULL AND spp.status = 'published' AND spp.slug = :speaker)"
    )


def _native_branches(q, video_id, filters, params):
    """WHERE clauses for native transcript hits and title-only native hits."""
    text_match, highlighted_text, text_rank, title_match = _match_expressions("s", q, filters, params)
    where_clauses = []
    if video_id:
        where_clauses.append("s.video_id = :vid")
        params["vid"] = str(video_id)
    where_clauses.extend(_video_filter_clauses(filters, params))
    if filters.get("has_speaker_labels") is not None:
        if filters["has_speaker_labels"]:
            where_clauses.append("s.speaker_label IS NOT NULL")
        else:
            where_clauses.append("s.speaker_label IS NULL")
    # The cross-table OR prevented the GIN predicate from narrowing the
    # transcript scan. Disjoint branches retain every title-only segment
    # and avoid duplicating segments which match both text and title.
    text_where = [text_match, *where_clauses]
    title_where = [title_match, f"({text_match}) IS NOT TRUE", *where_clauses]
    if filters.get("speaker"):
        text_where.append(_speaker_clause(filters, params))
        title_where.append(_speaker_clause(filters, params, "v.id"))
    return text_where, title_where, (text_match, highlighted_text, text_rank, title_match)


def _youtube_branches(q, video_id, filters, params):
    """WHERE clauses for caption hits and per-video caption title hits."""
    text_match, highlighted_text, text_rank, title_match = _match_expressions("ys", q, filters, params)
    # Keep transcript text search separate from title/youtube_id metadata search.
    # Applying a title match directly to every youtube_segments row makes title
    # searches scan/return every caption segment for matching videos.
    text_where = [text_match]
    title_where = [f"({title_match} OR v.youtube_id ILIKE :title_q)"]
    if filters.get("speaker"):
        # Captions carry no speakers.
        text_where.append("FALSE")
        title_where.append("FALSE")
    if video_id:
        text_where.append("yt.video_id = :vid")
        title_where.append("yt.video_id = :vid")
        params["vid"] = str(video_id)
    video_filters = _video_filter_clauses(filters, params)
    text_where.extend(video_filters)
    title_where.extend(video_filters)
    return text_where, title_where, (text_match, highlighted_text, text_rank, title_match)


# Captions count only for videos without a native transcript. The
# ``overshadowed`` flag marks the rest, and the caption full-text indexes are
# partial on ``NOT overshadowed`` so they never return lines search would drop.
_CAPTION_ONLY_LINE = "NOT ys.overshadowed"


def _best_branches(q, video_id, filters, params):
    """WHERE clauses for the four best-source branches: native and caption-only videos."""
    native_match, native_highlight, native_rank, title_match = _match_expressions("s", q, filters, params)
    youtube_match, youtube_highlight, youtube_rank, _ = _match_expressions("ys", q, filters, params)
    native_where = [native_match]
    native_title_where = [title_match]
    youtube_where = [youtube_match]
    youtube_title_where = [f"({title_match} OR v.youtube_id ILIKE :title_q)"]

    if video_id:
        native_where.append("s.video_id = :vid")
        native_title_where.append("v.id = :vid")
        youtube_where.append("yt.video_id = :vid")
        youtube_title_where.append("yt.video_id = :vid")
        params["vid"] = str(video_id)

    video_filters = _video_filter_clauses(filters, params)
    for clauses in (native_where, native_title_where, youtube_where, youtube_title_where):
        clauses.extend(video_filters)
    if filters.get("has_speaker_labels") is not None:
        if filters["has_speaker_labels"]:
            native_where.append("s.speaker_label IS NOT NULL")
            native_title_where.append("s.speaker_label IS NOT NULL")
            youtube_where.append("FALSE")
        else:
            native_where.append("s.speaker_label IS NULL")
            native_title_where.append("s.speaker_label IS NULL")
    if filters.get("speaker"):
        native_where.append(_speaker_clause(filters, params))
        native_title_where.append(_speaker_clause(filters, params, "v.id"))
        youtube_where.append("FALSE")
        youtube_title_where.append("FALSE")

    youtube_where.append(_CAPTION_ONLY_LINE)
    youtube_title_where.append("NOT yt.overshadowed")
    return (
        native_where,
        native_title_where,
        youtube_where,
        youtube_title_where,
        (native_match, native_highlight, native_rank, title_match),
        (youtube_match, youtube_highlight, youtube_rank),
    )


# Very common words such as "going" match hundreds of thousands of moments,
# and ranking them all reads most of the transcript tables. When the planner
# expects more matches than this in a branch, an unfiltered relevance search
# ranks only the matches from the newest broadcasts: just enough videos to hold
# about this many matches.
COMMON_QUERY_MATCHES = 20_000

_NEWEST_NATIVE_VIDEOS = """s.video_id = ANY(ARRAY(
                    SELECT recent.id FROM videos recent
                    WHERE EXISTS (SELECT 1 FROM segments recent_s WHERE recent_s.video_id = recent.id)
                    ORDER BY recent.broadcast_date DESC NULLS LAST, recent.id DESC
                    LIMIT :native_recent_videos
                ))"""

_NEWEST_CAPTION_TRANSCRIPTS = """ys.youtube_transcript_id = ANY(ARRAY(
                    SELECT recent.id FROM youtube_transcripts recent
                    JOIN videos recent_v ON recent_v.id = recent.video_id
                    WHERE NOT recent.overshadowed
                    ORDER BY recent_v.broadcast_date DESC NULLS LAST, recent_v.id DESC
                    LIMIT :caption_recent_videos
                ))"""


def _estimated_matches(db, from_clause: str, where: list[str], params: dict[str, Any]) -> float:
    """Return the planner's row estimate; planning alone takes about a millisecond."""
    sql = f"EXPLAIN (FORMAT JSON) SELECT 1 FROM {from_clause} WHERE {' AND '.join(where)}"
    plan = _execute_search(db, sql, params).scalar()
    if isinstance(plan, str):
        plan = json.loads(plan)
    return float(plan[0]["Plan"]["Plan Rows"])


def _cap_common_query(db, native_where, youtube_where, params) -> None:
    """Limit each text branch to its newest videos when the query is very common."""
    native_estimate = _estimated_matches(db, "segments s", native_where, params)
    caption_estimate = _estimated_matches(db, "youtube_segments ys", youtube_where, params)
    if max(native_estimate, caption_estimate) <= COMMON_QUERY_MATCHES:
        return
    native_videos, caption_videos = _execute_search(
        db,
        """SELECT
                (SELECT count(*) FROM videos v WHERE EXISTS (SELECT 1 FROM segments s WHERE s.video_id = v.id)),
                (SELECT count(*) FROM youtube_transcripts yt WHERE NOT yt.overshadowed)""",
        {},
    ).one()
    if native_estimate > COMMON_QUERY_MATCHES:
        params["native_recent_videos"] = math.ceil(native_videos * COMMON_QUERY_MATCHES / native_estimate)
        native_where.append(_NEWEST_NATIVE_VIDEOS)
    if caption_estimate > COMMON_QUERY_MATCHES:
        params["caption_recent_videos"] = math.ceil(caption_videos * COMMON_QUERY_MATCHES / caption_estimate)
        youtube_where.append(_NEWEST_CAPTION_TRANSCRIPTS)


_FIRST_NATIVE_SEGMENT = """
                JOIN LATERAL (
                    SELECT segment.id, segment.start_ms, segment.end_ms, segment.text, segment.speaker_label
                    FROM segments segment
                    WHERE segment.video_id = v.id
                    ORDER BY segment.start_ms ASC
                    LIMIT 1
                ) s ON true"""

_FIRST_CAPTION_SEGMENT = """
                JOIN LATERAL (
                    SELECT segment.id, segment.start_ms, segment.end_ms, segment.text
                    FROM youtube_segments segment
                    WHERE segment.youtube_transcript_id = yt.id
                    ORDER BY segment.start_ms ASC
                    LIMIT 1
                ) ys ON true"""


class SearchRepository:
    def search_native(
        self,
        db,
        q: str,
        video_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "relevance",
        filters: dict[str, Any] | None = None,
    ):
        from app.metrics import search_queries_total

        filters = filters or {}

        params = {
            "q": q,
            "limit": limit,
            "offset": offset,
            "branch_limit": limit + offset + 1,
            "headline_options": POSTGRES_HEADLINE_OPTIONS,
        }
        params["title_q"] = f"%{q.strip()}%"
        text_where, title_where, (text_match, highlighted_text, text_rank, title_match) = _native_branches(
            q, video_id, filters, params
        )

        # Build ORDER BY clause
        if sort_by == "date_desc":
            order_by = "v.broadcast_date DESC NULLS LAST, s.start_ms ASC"
        elif sort_by == "date_asc":
            order_by = "v.broadcast_date ASC NULLS LAST, s.start_ms ASC"
        elif sort_by == "duration_desc":
            order_by = "v.duration_seconds DESC NULLS LAST, s.start_ms ASC"
        elif sort_by == "duration_asc":
            order_by = "v.duration_seconds ASC NULLS LAST, s.start_ms ASC"
        else:  # relevance (default)
            order_by = "rank DESC, title_match DESC, s.start_ms ASC"

        select_fields = (
            "s.id, s.video_id, s.start_ms, s.end_ms, "
            f"CASE WHEN {text_match} "
            f"THEN {highlighted_text} "
            "ELSE coalesce(v.title, s.text) END AS snippet, "
            f"{text_rank} AS rank, "
            f"CASE WHEN {title_match} THEN 1 ELSE 0 END AS title_match"
        )
        from_clause = "segments s JOIN videos v ON s.video_id = v.id"

        result_order = order_by.replace("v.", "").replace("s.", "")
        sql = f"""
            WITH native_text_hits AS (
                SELECT {select_fields}, v.uploaded_at, v.broadcast_date, v.duration_seconds
                FROM {from_clause}
                WHERE {" AND ".join(text_where)}
                ORDER BY {order_by}
                LIMIT :branch_limit
            ), native_title_hits AS (
                SELECT {select_fields}, v.uploaded_at, v.broadcast_date, v.duration_seconds
                FROM {from_clause}
                WHERE {" AND ".join(title_where)}
                ORDER BY {order_by}
                LIMIT :branch_limit
            )
            SELECT id, video_id, start_ms, end_ms, snippet, rank, title_match
            FROM (
                SELECT * FROM native_text_hits
                UNION ALL
                SELECT * FROM native_title_hits
            ) native_hits
            ORDER BY {result_order}
            LIMIT :limit OFFSET :offset
        """

        rows = _execute_search(db, sql, params).mappings().all()

        # Track search query metric
        search_queries_total.labels(backend="postgres").inc()

        return normalize_search_rows(rows)

    def search_youtube(
        self,
        db,
        q: str,
        video_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "relevance",
        filters: dict[str, Any] | None = None,
    ):
        filters = filters or {}

        params = {
            "q": q,
            "limit": limit,
            "offset": offset,
            # Each branch is bounded before a UNION so a common query cannot
            # force a full-corpus sort just to produce one page.
            "branch_limit": limit + offset + 1,
            "title_q": f"%{q.strip()}%",
            "headline_options": POSTGRES_HEADLINE_OPTIONS,
        }
        text_where, title_where, (_text_match, highlighted_text, text_rank, _title_match) = _youtube_branches(
            q, video_id, filters, params
        )

        # Build ORDER BY clause
        if sort_by == "date_desc":
            order_by = "broadcast_date DESC NULLS LAST, start_ms ASC"
        elif sort_by == "date_asc":
            order_by = "broadcast_date ASC NULLS LAST, start_ms ASC"
        elif sort_by == "duration_desc":
            order_by = "duration_seconds DESC NULLS LAST, start_ms ASC"
        elif sort_by == "duration_asc":
            order_by = "duration_seconds ASC NULLS LAST, start_ms ASC"
        else:  # relevance (default)
            order_by = "title_match DESC, rank DESC, start_ms ASC"

        sql = f"""
            WITH text_hits AS (
                SELECT
                    ys.id,
                    yt.video_id,
                    ys.start_ms,
                    ys.end_ms,
                    {highlighted_text} AS snippet,
                    {text_rank} AS rank,
                    0 AS title_match,
                    v.uploaded_at,
                    v.broadcast_date,
                    v.duration_seconds,
                    v.title AS video_title,
                    v.channel_name
                FROM youtube_segments ys
                JOIN youtube_transcripts yt ON yt.id = ys.youtube_transcript_id
                JOIN videos v ON yt.video_id = v.id
                WHERE {" AND ".join(text_where)}
                ORDER BY {order_by}
                LIMIT :branch_limit
            ), title_candidates AS (
                SELECT DISTINCT ON (yt.video_id)
                    ys.id,
                    yt.video_id,
                    ys.start_ms,
                    ys.end_ms,
                    coalesce(v.title, ys.text) AS snippet,
                    0.0 AS rank,
                    1 AS title_match,
                    v.uploaded_at,
                    v.broadcast_date,
                    v.duration_seconds,
                    v.title AS video_title,
                    v.channel_name
                FROM youtube_transcripts yt
                JOIN videos v ON yt.video_id = v.id{_FIRST_CAPTION_SEGMENT}
                WHERE {" AND ".join(title_where)}
                ORDER BY yt.video_id, ys.start_ms ASC
            ), title_hits AS (
                SELECT * FROM title_candidates
                ORDER BY {order_by}
                LIMIT :branch_limit
            )
            SELECT id, video_id, start_ms, end_ms, snippet, rank, title_match,
                   uploaded_at, broadcast_date, duration_seconds, video_title, channel_name
            FROM (
                SELECT * FROM text_hits
                UNION ALL
                SELECT * FROM title_hits
            ) youtube_hits
            ORDER BY {order_by}
            LIMIT :limit OFFSET :offset
        """

        rows = _execute_search(db, sql, params).mappings().all()
        return normalize_search_rows(rows)

    def search_best(
        self,
        db,
        q: str,
        video_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "relevance",
        filters: dict[str, Any] | None = None,
    ):
        from app.metrics import search_queries_total

        filters = filters or {}
        params = {
            "q": q,
            "limit": limit,
            "offset": offset,
            "branch_limit": limit + offset + 1,
            "title_q": f"%{q.strip()}%",
            "headline_options": POSTGRES_HEADLINE_OPTIONS,
        }
        (
            native_where,
            native_title_where,
            youtube_where,
            youtube_title_where,
            (native_match, native_highlight, native_rank, title_match),
            (youtube_match, youtube_highlight, youtube_rank),
        ) = _best_branches(q, video_id, filters, params)
        if sort_by == "relevance" and not video_id and not _video_filter_clauses(filters, {}):
            _cap_common_query(db, native_where, youtube_where, params)

        if sort_by == "date_desc":
            order_by = "broadcast_date DESC NULLS LAST, start_ms ASC"
        elif sort_by == "date_asc":
            order_by = "broadcast_date ASC NULLS LAST, start_ms ASC"
        elif sort_by == "duration_desc":
            order_by = "duration_seconds DESC NULLS LAST, start_ms ASC"
        elif sort_by == "duration_asc":
            order_by = "duration_seconds ASC NULLS LAST, start_ms ASC"
        else:
            order_by = "rank DESC, title_match DESC, start_ms ASC"

        sql = f"""
            WITH native_text_hits AS (
                SELECT
                    s.id,
                    s.video_id,
                    s.start_ms,
                    s.end_ms,
                    CASE WHEN {native_match}
                        THEN {native_highlight}
                        ELSE coalesce(v.title, s.text)
                    END AS snippet,
                    'whisper' AS source,
                    {native_rank} AS rank,
                    CASE WHEN {title_match} THEN 1 ELSE 0 END AS title_match,
                    v.uploaded_at,
                    v.broadcast_date,
                    v.duration_seconds,
                    v.title AS video_title,
                    v.channel_name
                FROM segments s
                JOIN videos v ON s.video_id = v.id
                WHERE {" AND ".join(native_where)}
                ORDER BY {order_by}
                LIMIT :branch_limit
            ), native_title_hits AS (
                SELECT
                    s.id,
                    v.id AS video_id,
                    s.start_ms,
                    s.end_ms,
                    coalesce(v.title, s.text) AS snippet,
                    'whisper' AS source,
                    0.0 AS rank,
                    1 AS title_match,
                    v.uploaded_at,
                    v.broadcast_date,
                    v.duration_seconds,
                    v.title AS video_title,
                    v.channel_name
                FROM videos v{_FIRST_NATIVE_SEGMENT}
                WHERE {" AND ".join(native_title_where)}
                ORDER BY {order_by}
                LIMIT :branch_limit
            ), youtube_text_hits AS (
                SELECT
                    ys.id,
                    yt.video_id,
                    ys.start_ms,
                    ys.end_ms,
                    CASE WHEN {youtube_match}
                        THEN {youtube_highlight}
                        ELSE coalesce(v.title, ys.text)
                    END AS snippet,
                    'youtube' AS source,
                    {youtube_rank} AS rank,
                    CASE WHEN {title_match} OR v.youtube_id ILIKE :title_q THEN 1 ELSE 0 END AS title_match,
                    v.uploaded_at,
                    v.broadcast_date,
                    v.duration_seconds,
                    v.title AS video_title,
                    v.channel_name
                FROM youtube_segments ys
                JOIN youtube_transcripts yt ON yt.id = ys.youtube_transcript_id
                JOIN videos v ON yt.video_id = v.id
                WHERE {" AND ".join(youtube_where)}
                ORDER BY {order_by}
                LIMIT :branch_limit
            ), youtube_title_candidates AS (
                SELECT DISTINCT ON (yt.video_id)
                    ys.id,
                    yt.video_id,
                    ys.start_ms,
                    ys.end_ms,
                    coalesce(v.title, ys.text) AS snippet,
                    'youtube' AS source,
                    0.0 AS rank,
                    1 AS title_match,
                    v.uploaded_at,
                    v.broadcast_date,
                    v.duration_seconds,
                    v.title AS video_title,
                    v.channel_name
                FROM youtube_transcripts yt
                JOIN videos v ON yt.video_id = v.id{_FIRST_CAPTION_SEGMENT}
                WHERE {" AND ".join(youtube_title_where)}
                ORDER BY video_id, start_ms ASC
            ), youtube_title_hits AS (
                SELECT * FROM youtube_title_candidates
                ORDER BY {order_by}
                LIMIT :branch_limit
            )
            SELECT id, video_id, start_ms, end_ms, snippet, source, rank, title_match,
                   uploaded_at, broadcast_date, duration_seconds, video_title, channel_name
            FROM (
                SELECT * FROM native_text_hits
                UNION ALL
                SELECT * FROM native_title_hits
                UNION ALL
                SELECT * FROM youtube_text_hits
                UNION ALL
                SELECT * FROM youtube_title_hits
            ) best_hits
            ORDER BY {order_by}
            LIMIT :limit OFFSET :offset
        """
        rows = _execute_search(db, sql, params).mappings().all()
        search_queries_total.labels(backend="postgres").inc()
        return normalize_search_rows(rows)

    def mention_counts(
        self,
        db,
        q: str,
        source: str = "best",
        video_id: str | None = None,
        filters: dict[str, Any] | None = None,
    ):
        """Count every matched moment per video in one pass.

        The branches mirror the search methods so the counts agree with paging
        through the complete result set, without ranking or highlighting it.
        ``spoken`` excludes title-only moments, and the ``*_spoken_ms`` columns
        bound the spoken ones.
        """
        filters = filters or {}
        params: dict[str, Any] = {"q": q, "title_q": f"%{q.strip()}%"}
        if source == "best":
            native_where, native_title_where, youtube_where, youtube_title_where, _native, _youtube = _best_branches(
                q, video_id, filters, params
            )
            branches = [
                f"""SELECT s.video_id, s.start_ms, false AS title_only
                FROM segments s JOIN videos v ON s.video_id = v.id
                WHERE {" AND ".join(native_where)}""",
                f"""SELECT v.id AS video_id, s.start_ms, true AS title_only
                FROM videos v{_FIRST_NATIVE_SEGMENT}
                WHERE {" AND ".join(native_title_where)}""",
                f"""SELECT yt.video_id, ys.start_ms, false AS title_only
                FROM youtube_segments ys
                JOIN youtube_transcripts yt ON yt.id = ys.youtube_transcript_id
                JOIN videos v ON yt.video_id = v.id
                WHERE {" AND ".join(youtube_where)}""",
                f"""SELECT DISTINCT ON (yt.video_id) yt.video_id, ys.start_ms, true AS title_only
                FROM youtube_transcripts yt
                JOIN videos v ON yt.video_id = v.id{_FIRST_CAPTION_SEGMENT}
                WHERE {" AND ".join(youtube_title_where)}
                ORDER BY yt.video_id, ys.start_ms ASC""",
            ]
        elif source == "native":
            text_where, title_where, _expressions = _native_branches(q, video_id, filters, params)
            branches = [
                f"""SELECT s.video_id, s.start_ms, false AS title_only
                FROM segments s JOIN videos v ON s.video_id = v.id
                WHERE {" AND ".join(text_where)}""",
                f"""SELECT s.video_id, s.start_ms, true AS title_only
                FROM segments s JOIN videos v ON s.video_id = v.id
                WHERE {" AND ".join(title_where)}""",
            ]
        else:
            text_where, title_where, _expressions = _youtube_branches(q, video_id, filters, params)
            branches = [
                f"""SELECT yt.video_id, ys.start_ms, false AS title_only
                FROM youtube_segments ys
                JOIN youtube_transcripts yt ON yt.id = ys.youtube_transcript_id
                JOIN videos v ON yt.video_id = v.id
                WHERE {" AND ".join(text_where)}""",
                f"""SELECT DISTINCT ON (yt.video_id) yt.video_id, ys.start_ms, true AS title_only
                FROM youtube_transcripts yt
                JOIN videos v ON yt.video_id = v.id{_FIRST_CAPTION_SEGMENT}
                WHERE {" AND ".join(title_where)}
                ORDER BY yt.video_id, ys.start_ms ASC""",
            ]
        union = "\n                UNION ALL\n                ".join(f"({branch})" for branch in branches)
        sql = f"""
            WITH hits AS (
                {union}
            )
            SELECT hits.video_id,
                   v.broadcast_date,
                   v.created_at,
                   count(*) AS moments,
                   count(*) FILTER (WHERE NOT hits.title_only) AS spoken,
                   min(hits.start_ms) AS first_any_ms,
                   max(hits.start_ms) AS last_any_ms,
                   min(hits.start_ms) FILTER (WHERE NOT hits.title_only) AS first_spoken_ms,
                   max(hits.start_ms) FILTER (WHERE NOT hits.title_only) AS last_spoken_ms
            FROM hits
            JOIN videos v ON v.id = hits.video_id
            GROUP BY hits.video_id, v.broadcast_date, v.created_at
        """
        return _execute_search(db, sql, params).mappings().all()
