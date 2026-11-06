import functools
import json
import re
import time
import uuid
from datetime import date

from sqlalchemy import bindparam, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.archive.broadcast_dates import BROADCAST_AT_SQL, video_broadcast_at
from app.archive.repository import archive_repository
from app.archive.video_metadata_repository import get_video_metadata_map
from app.cache import cache
from app.logging_config import get_logger
from app.search.highlights import POSTGRES_HEADLINE_OPTIONS, normalize_search_rows
from app.search.segment_repository import SearchRepository
from app.settings import settings

logger = get_logger(__name__)
_search_repository = SearchRepository()

# Database retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 0.5  # seconds


def _retry_on_transient_error(func):
    """Decorator to retry database operations on transient errors."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except OperationalError as e:
                last_error = e
                db = args[0] if args else kwargs.get("db")
                if hasattr(db, "rollback"):
                    db.rollback()
                # PostgreSQL uses the same cancellation state for an explicit
                # cancel and statement_timeout. Repeating that expensive query
                # defeats the caller's deadline and keeps its admission slot.
                sqlstate = getattr(e.orig, "sqlstate", None) or getattr(e.orig, "pgcode", None)
                if sqlstate == "57014":
                    logger.info("Database query cancelled", extra={"attempts": attempt + 1, "sqlstate": sqlstate})
                    raise
                error_msg = str(e).lower()
                # Check if it's a transient error worth retrying
                if any(
                    pattern in error_msg
                    for pattern in ["connection", "timeout", "deadlock", "could not connect", "server closed"]
                ):
                    if attempt < MAX_RETRIES - 1:
                        delay = RETRY_DELAY * (2**attempt)  # Exponential backoff
                        logger.warning(
                            "Database transient error - retrying",
                            extra={
                                "attempt": attempt + 1,
                                "max_retries": MAX_RETRIES,
                                "retry_delay": delay,
                                "error": str(e),
                            },
                        )
                        time.sleep(delay)
                        continue
                # Not a transient error or max retries reached
                logger.error(
                    "Database error after retries",
                    extra={"attempts": attempt + 1, "error": str(e)},
                )
                raise
            except Exception:
                # For non-transient errors, raise immediately
                raise
        # Should not reach here, but just in case
        if last_error:
            raise last_error

    return wrapper


def normalize_job_ownership_meta(
    meta: dict | None, *, owner_user_id: uuid.UUID | str | None = None, api_key_id: uuid.UUID | str | None = None
) -> dict:
    """Remove caller-controlled ownership keys and derive them from FK/auth data."""
    normalized = dict(meta or {})
    normalized.pop("owner_user_id", None)
    normalized.pop("api_key_id", None)
    if owner_user_id is not None:
        normalized["owner_user_id"] = str(owner_user_id)
    if api_key_id is not None:
        normalized["api_key_id"] = str(api_key_id)
    return normalized


@_retry_on_transient_error
def create_job(
    db,
    kind: str,
    url: str,
    meta: dict | None = None,
    *,
    owner_user_id: uuid.UUID | str | None = None,
    api_key_id: uuid.UUID | str | None = None,
):
    import json

    from app.metrics import jobs_created_total

    job_id = uuid.uuid4()
    meta_json = json.dumps(normalize_job_ownership_meta(meta, owner_user_id=owner_user_id, api_key_id=api_key_id))
    db.execute(
        text("INSERT INTO jobs (id, kind, input_url, meta, owner_user_id) VALUES (:i,:k,:u,:m,:owner)"),
        {"i": str(job_id), "k": kind, "u": url, "m": meta_json, "owner": str(owner_user_id) if owner_user_id else None},
    )
    db.commit()

    # Track job creation metric
    jobs_created_total.labels(kind=kind).inc()

    return job_id


@_retry_on_transient_error
def fetch_job(db, job_id: uuid.UUID):
    # Pass UUID directly to ensure proper binding/casting
    row = db.execute(text("SELECT * FROM jobs WHERE id=:i"), {"i": job_id}).mappings().first()
    return row


@_retry_on_transient_error
@cache(
    prefix="segments",
    ttl=settings.CACHE_TRANSCRIPT_TTL if settings.ENABLE_CACHING else 0,
    should_cache=bool,
)
def list_segments(db, video_id):
    # Keep the common direct-video lookup indexable. Combining this with the
    # legacy transcript relationship via OR forced PostgreSQL to scan the full
    # segments table for every transcript request.
    params = {"v": str(video_id)}
    direct_rows = db.execute(
        text("""
            SELECT s.start_ms, s.end_ms, s.text, s.speaker_label
            FROM segments s
            WHERE s.video_id = :v
            ORDER BY s.start_ms
            """),
        params,
    ).all()
    if direct_rows:
        return [list(row) for row in direct_rows]

    # Compatibility fallback for older rows linked only through transcripts.
    rows = db.execute(
        text("""
            SELECT s.start_ms, s.end_ms, s.text, s.speaker_label
            FROM segments s
            JOIN transcripts t ON t.id = s.transcript_id
            WHERE t.video_id = :v
            ORDER BY s.start_ms
            """),
        params,
    ).all()
    return [list(row) for row in rows]


@_retry_on_transient_error
@cache(
    prefix="video",
    ttl=settings.CACHE_VIDEO_TTL if settings.ENABLE_CACHING else 0,
    should_cache=lambda value: isinstance(value, dict) and value.get("state") == "completed",
)
def get_video(db, video_id: uuid.UUID):
    row = (
        db.execute(
            text("""
                SELECT
                    v.id, v.youtube_id, v.title, v.duration_seconds, v.state,
                    v.caption_ingest_state, v.diarization_state, v.uploaded_at,
                    v.broadcast_date, v.broadcast_date_source, v.broadcast_kind,
                    v.created_at, v.updated_at, v.channel_name, v.language, v.category,
                    EXISTS (SELECT 1 FROM segments s WHERE s.video_id = v.id) AS has_whisper_transcript,
                    EXISTS (SELECT 1 FROM youtube_transcripts yt WHERE yt.video_id = v.id) AS has_youtube_transcript
                FROM videos v
                WHERE v.id=:v
                """),
            {"v": str(video_id)},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    metadata_map = _safe_video_metadata_map(db, [video_id])
    payload = dict(row)
    metadata = metadata_map.get(str(video_id), {"people": [], "tags": []})
    payload["people"] = metadata.get("people", [])
    payload["tags"] = metadata.get("tags", [])
    return payload


VIDEO_DATE_FIELDS = {"broadcast_date", "uploaded_at", "created_at", "updated_at"}
# Newest-first ordering key: the broadcast date, then ingestion time for undated rows.
VIDEO_EFFECTIVE_AT_SQL = f"COALESCE({BROADCAST_AT_SQL}, v.created_at)"


def _video_date_filters(date_column: str, date_from, date_to, params: dict[str, object]) -> list[str]:
    """Inclusive calendar-date filters; broadcast_date is a DATE, the rest are timestamps."""

    clauses = []
    if date_column == "broadcast_date":
        if date_from is not None:
            clauses.append("v.broadcast_date >= CAST(:date_from AS date)")
            params["date_from"] = date_from
        if date_to is not None:
            clauses.append("v.broadcast_date <= CAST(:date_to AS date)")
            params["date_to"] = date_to
        return clauses
    if date_from is not None:
        clauses.append(f"v.{date_column} >= CAST(:date_from AS timestamptz)")
        params["date_from"] = date_from
    if date_to is not None:
        clauses.append(f"v.{date_column} < CAST(:date_to AS timestamptz) + INTERVAL '1 day'")
        params["date_to"] = date_to
    return clauses


@_retry_on_transient_error
def list_videos(
    db,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    date_field: str = "broadcast_date",
    date_from=None,
    date_to=None,
    completed_only: bool = False,
    category: str | None = None,
):
    date_column = date_field if date_field in VIDEO_DATE_FIELDS else "broadcast_date"
    where_clauses = []
    params: dict[str, object] = {"limit": limit, "offset": offset}

    if q:
        where_clauses.append("(v.title ILIKE :q OR v.youtube_id ILIKE :q OR v.channel_name ILIKE :q)")
        params["q"] = f"%{q}%"
    where_clauses.extend(_video_date_filters(date_column, date_from, date_to, params))
    if completed_only:
        where_clauses.append("v.state = 'completed'")
    if category:
        where_clauses.append("v.category ILIKE :category")
        params["category"] = category

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = f"""
        SELECT
            v.id, v.youtube_id, v.title, v.duration_seconds, v.state,
            v.caption_ingest_state, v.diarization_state, v.uploaded_at,
                    v.broadcast_date, v.broadcast_date_source, v.broadcast_kind,
            v.created_at, v.updated_at, v.channel_name, v.language, v.category,
            EXISTS (SELECT 1 FROM segments s WHERE s.video_id = v.id) AS has_whisper_transcript,
            EXISTS (SELECT 1 FROM youtube_transcripts yt WHERE yt.video_id = v.id) AS has_youtube_transcript
        FROM videos v
        {where_sql}
        ORDER BY {VIDEO_EFFECTIVE_AT_SQL} DESC, v.uploaded_at DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """
    rows = db.execute(text(sql), params).mappings().all()
    metadata_map = _safe_video_metadata_map(db, [row["id"] for row in rows])
    return _attach_video_metadata_rows(rows, metadata_map)


def _video_feed_filters(
    *,
    q: str | None = None,
    date_field: str = "broadcast_date",
    date_from=None,
    date_to=None,
    completed_only: bool = False,
    category: str | None = None,
    people: list[str] | None = None,
    tags: list[str] | None = None,
    min_duration: int | None = None,
    max_duration: int | None = None,
    transcript_source: str = "any",
):
    date_column = date_field if date_field in VIDEO_DATE_FIELDS else "broadcast_date"
    where_clauses: list[str] = []
    params: dict[str, object] = {}
    if q:
        where_clauses.append("(v.title ILIKE :q OR v.youtube_id ILIKE :q OR v.channel_name ILIKE :q)")
        params["q"] = f"%{q}%"
        params["q_exact"] = q
        params["q_prefix"] = f"{q}%"
    where_clauses.extend(_video_date_filters(date_column, date_from, date_to, params))
    if completed_only:
        where_clauses.append("v.state = 'completed'")
    if category:
        where_clauses.append("v.category ILIKE :category")
        params["category"] = category
    if people:
        where_clauses.append(
            "(EXISTS (SELECT 1 FROM archive_video_people vp JOIN archive_people p ON p.id=vp.person_id "
            "WHERE vp.video_id=v.id AND p.status='published' AND p.slug = ANY(CAST(:people AS text[]))) "
            "OR EXISTS (SELECT 1 FROM archive_label_assignments a JOIN archive_labels l ON l.id=a.label_id "
            "WHERE a.video_id=v.id AND a.unit_type='vod' AND l.kind='person' AND l.status='published' "
            "AND l.slug = ANY(CAST(:people AS text[])) "
            "AND (a.status='admin_approved' OR (a.status='auto_published' AND a.publish_tier IN ('gold','silver'))) "
            "AND COALESCE(a.evidence->0->>'person_role','subject') IN ('guest','host','caller')))"
        )
        params["people"] = list(dict.fromkeys(people))
    if tags:
        where_clauses.append(
            "(EXISTS (SELECT 1 FROM archive_video_taggings vt JOIN archive_video_tags t ON t.id=vt.tag_id "
            "WHERE vt.video_id=v.id AND t.status='published' AND t.slug = ANY(CAST(:tags AS text[]))) "
            "OR EXISTS (SELECT 1 FROM archive_label_assignments a JOIN archive_labels l ON l.id=a.label_id "
            "WHERE a.video_id=v.id AND a.unit_type='vod' AND l.kind<>'person' AND l.status='published' "
            "AND l.slug = ANY(CAST(:tags AS text[])) "
            "AND (a.status='admin_approved' OR (a.status='auto_published' AND a.publish_tier IN ('gold','silver') "
            "AND l.kind IN ('category','series','game','meme','event')))))"
        )
        params["tags"] = list(dict.fromkeys(tags))
    if min_duration is not None:
        where_clauses.append("v.duration_seconds >= :min_duration")
        params["min_duration"] = min_duration
    if max_duration is not None:
        where_clauses.append("v.duration_seconds <= :max_duration")
        params["max_duration"] = max_duration
    if transcript_source in {"whisper", "both"}:
        where_clauses.append("EXISTS (SELECT 1 FROM segments s WHERE s.video_id=v.id)")
    if transcript_source in {"youtube", "both"}:
        where_clauses.append("EXISTS (SELECT 1 FROM youtube_transcripts yt WHERE yt.video_id=v.id)")
    return where_clauses, params


@_retry_on_transient_error
def list_video_feed(
    db,
    *,
    limit: int,
    sort: str,
    cursor_primary: str | int | float | None = None,
    cursor_secondary: str | int | float | None = None,
    cursor_id: uuid.UUID | None = None,
    **filters,
):
    """List one keyset page plus one lookahead row."""

    where_clauses, params = _video_feed_filters(**filters)
    effective_at = VIDEO_EFFECTIVE_AT_SQL
    relevance = "CASE WHEN lower(v.title)=lower(:q_exact) THEN 3 " "WHEN v.title ILIKE :q_prefix THEN 2 ELSE 1 END"
    if sort == "longest":
        primary = "COALESCE(v.duration_seconds, -1)"
        order_sql = f"{primary} DESC, v.id DESC"
        if cursor_id is not None:
            if cursor_primary is None:
                raise ValueError("longest cursor is missing its primary value")
            where_clauses.append(f"({primary}, v.id) < (:cursor_primary, :cursor_id)")
            params.update(cursor_primary=int(cursor_primary), cursor_id=str(cursor_id))
        select_cursor = f"{primary} AS feed_primary, NULL::text AS feed_secondary"
    elif sort == "relevance":
        order_sql = f"{relevance} DESC, {effective_at} DESC, v.id DESC"
        if cursor_id is not None:
            if cursor_primary is None:
                raise ValueError("relevance cursor is missing its primary value")
            where_clauses.append(
                f"({relevance}, {effective_at}, v.id) < "
                "(:cursor_primary, CAST(:cursor_secondary AS timestamptz), :cursor_id)"
            )
            params.update(
                cursor_primary=int(cursor_primary), cursor_secondary=cursor_secondary, cursor_id=str(cursor_id)
            )
        select_cursor = f"{relevance} AS feed_primary, {effective_at} AS feed_secondary"
    else:
        order_sql = f"{effective_at} DESC, v.id DESC"
        if cursor_id is not None:
            where_clauses.append(f"({effective_at}, v.id) < (CAST(:cursor_primary AS timestamptz), :cursor_id)")
            params.update(cursor_primary=cursor_primary, cursor_id=str(cursor_id))
        select_cursor = f"{effective_at} AS feed_primary, NULL::text AS feed_secondary"
    params["limit"] = limit + 1
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    rows = (
        db.execute(
            text(f"""
            SELECT v.id, v.youtube_id, v.title, v.duration_seconds, v.state,
                   v.caption_ingest_state, v.diarization_state, v.uploaded_at,
                    v.broadcast_date, v.broadcast_date_source, v.broadcast_kind,
                   v.created_at, v.updated_at, v.channel_name, v.language, v.category,
                   EXISTS (SELECT 1 FROM segments s WHERE s.video_id=v.id) AS has_whisper_transcript,
                   EXISTS (SELECT 1 FROM youtube_transcripts yt WHERE yt.video_id=v.id) AS has_youtube_transcript,
                   {select_cursor}
            FROM videos v
            {where_sql}
            ORDER BY {order_sql}
            LIMIT :limit
            """),
            params,
        )
        .mappings()
        .all()
    )
    metadata_map = _safe_video_metadata_map(db, [row["id"] for row in rows[:limit]])
    return _attach_video_metadata_rows(rows, metadata_map)


@_retry_on_transient_error
def count_videos(
    db,
    q: str | None = None,
    date_field: str = "broadcast_date",
    date_from=None,
    date_to=None,
    completed_only: bool = False,
    category: str | None = None,
    people: list[str] | None = None,
    tags: list[str] | None = None,
    min_duration: int | None = None,
    max_duration: int | None = None,
    transcript_source: str = "any",
):
    where_clauses, params = _video_feed_filters(
        q=q,
        date_field=date_field,
        date_from=date_from,
        date_to=date_to,
        completed_only=completed_only,
        category=category,
        people=people,
        tags=tags,
        min_duration=min_duration,
        max_duration=max_duration,
        transcript_source=transcript_source,
    )
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    return db.execute(text(f"SELECT count(*) AS count FROM videos v {where_sql}"), params).mappings().first()["count"]


def _video_info_rows_to_models(rows):
    from app.schemas import VideoInfo

    return [VideoInfo(**dict(row)) for row in rows]


def _attach_video_metadata_rows(rows, metadata_map: dict[str, dict[str, list[dict]]] | None = None):
    metadata_map = metadata_map or {}
    enriched = []
    for row in rows:
        payload = dict(row)
        metadata = metadata_map.get(str(payload.get("id")), {"people": [], "tags": []})
        payload["people"] = metadata.get("people", [])
        payload["tags"] = metadata.get("tags", [])
        enriched.append(payload)
    return enriched


def _safe_video_metadata_map(db, video_ids, published_only: bool = True):
    try:
        return get_video_metadata_map(db, video_ids, published_only=published_only)
    except (OperationalError, ProgrammingError, AssertionError):
        db.rollback()
        return {str(video_id): {"people": [], "tags": []} for video_id in video_ids}


@_retry_on_transient_error
def get_videos_by_ids(db, video_ids):
    if not video_ids:
        return []

    sql = text(f"""
        SELECT
            v.id, v.youtube_id, v.title, v.duration_seconds, v.state,
            v.caption_ingest_state, v.diarization_state, v.uploaded_at,
                    v.broadcast_date, v.broadcast_date_source, v.broadcast_kind,
            v.created_at, v.updated_at, v.channel_name, v.language, v.category,
            EXISTS (SELECT 1 FROM segments s WHERE s.video_id = v.id) AS has_whisper_transcript,
            EXISTS (SELECT 1 FROM youtube_transcripts yt WHERE yt.video_id = v.id) AS has_youtube_transcript
        FROM videos v
        WHERE v.id IN :video_ids
        ORDER BY {VIDEO_EFFECTIVE_AT_SQL} DESC, v.uploaded_at DESC NULLS LAST
        """).bindparams(bindparam("video_ids", expanding=True))
    rows = db.execute(sql, {"video_ids": [str(video_id) for video_id in video_ids]}).mappings().all()
    metadata_map = _safe_video_metadata_map(db, [row["id"] for row in rows])
    return _video_info_rows_to_models(_attach_video_metadata_rows(rows, metadata_map))


def _archive_video_filter_sql() -> str:
    return """
        EXISTS (SELECT 1 FROM segments s WHERE s.video_id = v.id)
        OR EXISTS (SELECT 1 FROM youtube_transcripts yt WHERE yt.video_id = v.id)
    """


@_retry_on_transient_error
def get_archive_summary(db, recent_limit: int = 6, popular_limit: int = 8):
    return archive_repository.get_summary(db, recent_limit=recent_limit, popular_limit=popular_limit)


@_retry_on_transient_error
def get_archive_timeline(
    db,
    limit: int = 100,
    granularity: str = "month",
    date_from: date | None = None,
    date_to: date | None = None,
):
    """Bucket archived videos by broadcast date.

    Counts, durations and coverage come from aggregate queries over every
    matching video; ``limit`` only caps how many videos are listed.
    "No stream" placeholders count toward coverage, never as videos.
    """

    from app.archive.broadcast_dates import coverage_for_range, load_day_index
    from app.archive.intelligence_queries.periods import bucket_bounds, bucket_period_and_label
    from app.schemas import ArchiveTimelineResponse, PeriodCoverage, TimelineBucket

    archive_filter = _archive_video_filter_sql()
    bucket_trunc = "year" if granularity == "year" else ("week" if granularity == "week" else "month")
    date_filters = []
    params: dict[str, object] = {"limit": limit}
    if date_from is not None:
        date_filters.append("v.broadcast_date >= CAST(:date_from AS DATE)")
        params["date_from"] = date_from
    if date_to is not None:
        date_filters.append("v.broadcast_date <= CAST(:date_to AS DATE)")
        params["date_to"] = date_to
    date_filter_sql = "" if not date_filters else f" AND {' AND '.join(date_filters)}"
    base_where = f"""
        WHERE ({archive_filter})
          AND v.broadcast_date IS NOT NULL
          AND v.broadcast_kind <> 'no_stream'
          {date_filter_sql}
    """
    bucket_sql = f"date_trunc('{bucket_trunc}', v.broadcast_date)::date"

    aggregates = (
        db.execute(
            text(f"""
                SELECT {bucket_sql} AS bucket_start,
                       count(*) AS video_count,
                       COALESCE(sum(v.duration_seconds), 0) AS total_duration_seconds
                FROM videos v
                {base_where}
                GROUP BY 1
                ORDER BY 1 DESC
                """),
            params,
        )
        .mappings()
        .all()
    )
    rows = (
        db.execute(
            text(f"""
                SELECT
                    v.id, v.youtube_id, v.title, v.duration_seconds, v.state,
                    v.caption_ingest_state, v.diarization_state, v.uploaded_at,
                    v.broadcast_date, v.broadcast_date_source, v.broadcast_kind,
                    v.created_at, v.updated_at, v.channel_name, v.language, v.category,
                    EXISTS (SELECT 1 FROM segments s WHERE s.video_id = v.id) AS has_whisper_transcript,
                    EXISTS (SELECT 1 FROM youtube_transcripts yt WHERE yt.video_id = v.id) AS has_youtube_transcript,
                    {bucket_sql} AS bucket_start
                FROM videos v
                {base_where}
                ORDER BY v.broadcast_date DESC, v.uploaded_at DESC NULLS LAST, v.created_at DESC
                LIMIT :limit
                """),
            params,
        )
        .mappings()
        .all()
    )
    metadata_map = _safe_video_metadata_map(db, [row["id"] for row in rows])
    day_index = load_day_index(db)

    buckets: dict[date, TimelineBucket] = {}
    for aggregate in aggregates:
        bucket_start = aggregate["bucket_start"]
        period, label = bucket_period_and_label(bucket_start, bucket_trunc)
        first_day, last_day = bucket_bounds(bucket_start, bucket_trunc)
        coverage = coverage_for_range(day_index, first_day, last_day)
        buckets[bucket_start] = TimelineBucket(
            period=period,
            label=label,
            video_count=int(aggregate["video_count"]),
            total_duration_seconds=int(aggregate["total_duration_seconds"]),
            coverage=PeriodCoverage(**coverage) if coverage else None,
            videos=[],
        )
    for row in rows:
        bucket = buckets.get(row["bucket_start"])
        if bucket is not None:
            bucket.videos.append(_video_info_rows_to_models(_attach_video_metadata_rows([row], metadata_map))[0])

    return ArchiveTimelineResponse(buckets=list(buckets.values()), query_time_ms=None)


def _search_rows_for_grouping(db, q, source, video_id, limit, offset, sort_by, filters):
    if source == "best":
        return search_best_segments_advanced(
            db,
            q=q,
            video_id=video_id,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            filters=filters,
        )
    if source == "native":
        return search_segments_advanced(
            db,
            q=q,
            video_id=video_id,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            filters=filters,
        )
    return search_youtube_segments_advanced(
        db,
        q=q,
        video_id=video_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        filters=filters,
    )


@_retry_on_transient_error
def get_grouped_search(
    db,
    q: str,
    source: str = "best",
    video_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "relevance",
    filters: dict | None = None,
):
    from app.pagination import build_offset_page
    from app.schemas import EpisodeSearchGroup, GroupedSearchResponse, SearchMoment

    filters = filters or {}
    start_time = time.time()
    # Ask the repository for one extra raw moment so pagination stays honest
    # without a second, expensive count query over the transcript corpus.
    rows, page_info = build_offset_page(
        _search_rows_for_grouping(db, q, source, video_id, limit + 1, offset, sort_by, filters),
        limit=limit,
        offset=offset,
    )
    video_ids = []
    for row in rows:
        row_vid = str(row["video_id"])
        if row_vid not in video_ids:
            video_ids.append(row_vid)
    video_map = {str(video.id): video for video in get_videos_by_ids(db, video_ids)}

    groups: dict[str, EpisodeSearchGroup] = {}
    for row in rows:
        vid = str(row["video_id"])
        video = video_map.get(vid)
        if video is None:
            continue
        group = groups.get(vid)
        if group is None:
            group = EpisodeSearchGroup(video=video, moments=[])
            groups[vid] = group
        group.moments.append(
            SearchMoment(
                id=int(row["id"]),
                video_id=video.id,
                start_ms=int(row["start_ms"]),
                end_ms=int(row["end_ms"]),
                snippet=row["snippet"] or "",
                highlights=row.get("highlights") or [],
                source=(
                    row["source"]
                    if row.get("source") in {"whisper", "youtube", "merged"}
                    else ("whisper" if source == "native" else "youtube")
                ),
                video_title=video.title,
                channel_name=video.channel_name,
                uploaded_at=video.uploaded_at,
                broadcast_date=video.broadcast_date,
                duration_seconds=video.duration_seconds,
                title_match=bool(row.get("title_match")),
            )
        )

    return GroupedSearchResponse(
        total_moments=len(rows),
        total_videos=len(groups),
        groups=list(groups.values()),
        query_time_ms=int((time.time() - start_time) * 1000),
        page_info=page_info,
    )


# Function words, discourse fillers and generic qualifiers that co-occur with
# every topic. Related terms should name a subject, not describe one.
_RELATED_TERM_STOPWORDS = frozenset("""
    about above actually after again against ahead almost along already also although always among
    another anybody anyone anything anyway anywhere around away basically because been before behind
    being below best better between beyond bigger biggest both bring brought called came cannot
    certainly chat coming could couldn't definitely didn't does doesn't doing done don't down during
    each early easier easiest easy either else enough even ever every everybody everyone everything
    everywhere exactly feel feels felt few find first five four from front further gave genuinely
    gets getting give given gives goes going gone gonna good gotta great guess guys happen happened
    happens hard have haven't having hear heard here high higher honestly however huge idea isn't
    just keep kept kind kinda know knows large last later least less let's like likely literally
    little long longer look looked looking looks lots made make makes making many matter maybe mean
    means meant might mine more most much must need needs never next nothing obviously okay once
    only other others ought over part people person place point pretty probably quite rather real
    really right said same saying says seem seemed seems seen several should shouldn't show since
    small some somebody someone something sometimes somewhere sort sorta start started still stuff
    such sure take taken takes taking talk talked talking tell than thank thanks that that's their
    theirs them then there there's these they they're thing things think thinks this those though
    thought three through time times today told took totally toward true truly trying twice under
    until upon used very want wanted wants wasn't watch week went were weren't what what's whatever
    when whenever where whether which while whole whose will with within without won't worse worst
    would wouldn't yeah years you'll your you're yours yourself it's he's she's i'm we're well fucking
    """.split())


def related_terms(q: str, moments, limit: int = 5) -> list[str]:
    """Return subject words that recur across cited transcript snippets.

    A term must appear in at least two distinct moments once the result set is
    large enough to make a single co-occurrence meaningless, and terms heard in
    more videos rank ahead of terms repeated inside one broadcast.
    """
    query_terms = {term.lower() for term in re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", q)}
    moment_counts: dict[str, int] = {}
    video_sets: dict[str, set[str]] = {}
    for moment in moments:
        seen: set[str] = set()
        for raw_term in re.findall(r"[A-Za-z][A-Za-z0-9'-]{3,}", moment.snippet or ""):
            term = raw_term.strip("'-").lower()
            if len(term) < 4 or term in _RELATED_TERM_STOPWORDS or term in query_terms:
                continue
            if any(term.startswith(query_term) or query_term.startswith(term) for query_term in query_terms):
                continue
            seen.add(term)
        for term in seen:
            moment_counts[term] = moment_counts.get(term, 0) + 1
            video_sets.setdefault(term, set()).add(str(moment.video_id))
    minimum = 2 if len(moments) >= 4 else 1
    ranked = sorted(
        ((term, count) for term, count in moment_counts.items() if count >= minimum),
        key=lambda item: (-len(video_sets[item[0]]), -item[1], item[0]),
    )
    return [term for term, _count in ranked[:limit]]


# Related terms come from the most-mentioned episodes. Each is a cheap per-video
# fetch, so a common term never ranks or highlights its whole result set, and a
# rare term's handful of episodes still contributes every moment.
RELATED_TERM_EPISODES = 20
RELATED_TERM_MOMENTS_PER_EPISODE = 50


def _pick_moment(moments, start_ms):
    """Return the moment at ``start_ms``, preferring a spoken (highlighted) one."""
    at_time = [moment for moment in moments if moment.start_ms == start_ms]
    spoken = [moment for moment in at_time if moment.highlights]
    return (spoken or at_time or [None])[0]


@_retry_on_transient_error
def get_mention_map(
    db,
    q: str,
    source: str = "best",
    video_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "relevance",
    filters: dict | None = None,
    top_limit: int = 5,
):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from app.schemas import EpisodeSearchGroup, MentionMap

    filters = filters or {}
    start_time = time.time()
    # Mention-map statistics describe the complete result set. Counting it per
    # video in one pass avoids ranking and highlighting every matched moment.
    counts = [dict(row) for row in _search_repository.mention_counts(db, q, source, video_id, filters)]
    baseline = datetime.min.replace(tzinfo=timezone.utc)
    for row in counts:
        row["video_id"] = str(row["video_id"])
        # Moments are dated by when the stream happened, not when it was uploaded.
        row["broadcast_at"] = video_broadcast_at(SimpleNamespace(**row))

    total_moments = sum(int(row["moments"]) for row in counts)
    period_counts: dict[str, int] = {}
    recent_mentions_90d = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    for row in counts:
        if row["broadcast_at"] is None:
            continue
        period = str(row["broadcast_at"].year)
        period_counts[period] = period_counts.get(period, 0) + int(row["moments"])
        if row["broadcast_at"] >= cutoff:
            recent_mentions_90d += int(row["moments"])
    most_discussed_period, most_discussed_count = (None, 0)
    if period_counts:
        most_discussed_period, most_discussed_count = sorted(
            period_counts.items(), key=lambda item: (-item[1], item[0])
        )[0]

    # Title matches cite a video, not a spoken passage: they count as matches but
    # never stand in as the first or latest mention unless nothing was spoken.
    spoken_rows = [row for row in counts if row["spoken"]]
    first_key, last_key = ("first_spoken_ms", "last_spoken_ms") if spoken_rows else ("first_any_ms", "last_any_ms")
    citable_rows = spoken_rows or counts
    first_row = min(
        citable_rows, key=lambda row: (row["broadcast_at"] or baseline, row[first_key], row["video_id"]), default=None
    )
    latest_row = max(
        citable_rows, key=lambda row: (row["broadcast_at"] or baseline, row[last_key], row["video_id"]), default=None
    )
    ranked_rows = sorted(
        counts, key=lambda row: (-int(row["moments"]), row["broadcast_at"] or baseline, row["video_id"])
    )
    top_rows = ranked_rows[:top_limit]
    # Top, first and latest episodes are shown in full; the rest only feed related terms.
    shown_rows = [row for row in (*top_rows, first_row, latest_row) if row is not None]
    shown = {row["video_id"] for row in shown_rows}
    evidence_rows = ranked_rows[: max(top_limit, RELATED_TERM_EPISODES)]

    groups: dict[str, EpisodeSearchGroup] = {}
    for row in [*shown_rows, *evidence_rows]:
        if row["video_id"] in groups:
            continue
        moments = int(row["moments"])
        page = get_grouped_search(
            db,
            q=q,
            source=source,
            video_id=row["video_id"],
            limit=moments if row["video_id"] in shown else min(moments, RELATED_TERM_MOMENTS_PER_EPISODE),
            sort_by="date_asc",
            filters=filters,
        )
        for group in page.groups:
            effective_date = video_broadcast_at(group.video)
            groups[str(group.video.id)] = group.model_copy(
                update={
                    "moments": [
                        moment.model_copy(
                            update={"uploaded_at": effective_date, "broadcast_date": group.video.broadcast_date}
                        )
                        for moment in group.moments
                    ]
                }
            )

    def cited(row, key):
        group = groups.get(row["video_id"]) if row else None
        return _pick_moment(group.moments, row[key]) if group else None

    first_mention = cited(first_row, first_key)
    latest_mention = cited(latest_row, last_key)
    first_mentioned_year = first_row["broadcast_at"].year if first_row and first_row["broadcast_at"] else None

    evidence = [moment for group in groups.values() for moment in group.moments if not moment.title_match]
    related_topics = related_terms(q, evidence)

    top_episodes = [groups[row["video_id"]] for row in top_rows if row["video_id"] in groups]

    return MentionMap(
        query=q,
        total_moments=total_moments,
        total_videos=len(counts),
        first_mentioned_year=first_mentioned_year,
        most_discussed_period=most_discussed_period,
        most_discussed_count=most_discussed_count,
        recent_mentions_90d=recent_mentions_90d,
        related_topics=related_topics,
        top_episodes_count=len(top_episodes),
        first_mention=first_mention,
        latest_mention=latest_mention,
        top_episodes=top_episodes,
        query_time_ms=int((time.time() - start_time) * 1000),
    )


@_retry_on_transient_error
def list_saved_searches(db, user_id):
    from app.schemas import SavedSearch, SavedSearchesResponse

    rows = (
        db.execute(
            text("""
                SELECT id, query, filters, created_at
                FROM saved_searches
                WHERE user_id = :user_id
                ORDER BY created_at DESC
                """),
            {"user_id": str(user_id)},
        )
        .mappings()
        .all()
    )
    return SavedSearchesResponse(
        items=[
            SavedSearch(id=row["id"], query=row["query"], filters=row["filters"] or {}, created_at=row["created_at"])
            for row in rows
        ]
    )


@_retry_on_transient_error
def create_saved_search(db, user_id, query: str, filters: dict | None = None):
    from app.schemas import SavedSearch

    payload = filters or {}
    row = (
        db.execute(
            text("""
                INSERT INTO saved_searches (id, user_id, query, filters)
                VALUES (:id, :user_id, :query, :filters)
                ON CONFLICT (user_id, query)
                DO UPDATE SET filters = EXCLUDED.filters,
                              created_at = now()
                RETURNING id, query, filters, created_at
                """),
            {"id": str(uuid.uuid4()), "user_id": str(user_id), "query": query.strip(), "filters": json.dumps(payload)},
        )
        .mappings()
        .first()
    )
    db.commit()
    return SavedSearch(id=row["id"], query=row["query"], filters=row["filters"] or {}, created_at=row["created_at"])


@_retry_on_transient_error
def delete_saved_search(db, user_id, saved_search_id):
    result = db.execute(
        text("DELETE FROM saved_searches WHERE id = :id AND user_id = :user_id"),
        {"id": str(saved_search_id), "user_id": str(user_id)},
    )
    db.commit()
    return result.rowcount > 0


@_retry_on_transient_error
def list_completed_videos(db, limit: int = 12, offset: int = 0):
    return (
        db.execute(
            text("""
            SELECT v.id, v.youtube_id, v.title, v.duration_seconds
            FROM videos v
            WHERE v.state = 'completed'
              AND EXISTS (SELECT 1 FROM segments s WHERE s.video_id = v.id)
            ORDER BY v.updated_at DESC, v.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
            {"limit": limit, "offset": offset},
        )
        .mappings()
        .all()
    )


@_retry_on_transient_error
def get_youtube_transcript(db, video_id):
    return (
        db.execute(
            text("SELECT id, language, kind, full_text FROM youtube_transcripts WHERE video_id=:v"),
            {"v": str(video_id)},
        )
        .mappings()
        .first()
    )


@_retry_on_transient_error
def list_youtube_segments(db, youtube_transcript_id):
    return db.execute(
        text("SELECT start_ms,end_ms,text FROM youtube_segments WHERE youtube_transcript_id=:t ORDER BY start_ms"),
        {"t": str(youtube_transcript_id)},
    ).all()


@_retry_on_transient_error
def replace_transcript_blocks(conn, video_id, blocks):
    conn.execute(text("DELETE FROM transcript_blocks WHERE video_id = :v"), {"v": str(video_id)})
    for block in blocks:
        conn.execute(
            text("""
                INSERT INTO transcript_blocks (
                    video_id, block_index, start_ms, end_ms, speaker_label,
                    text, segment_ids, kind, formatter_version
                )
                VALUES (
                    :video_id, :block_index, :start_ms, :end_ms, :speaker_label,
                    :text, :segment_ids, :kind, :formatter_version
                )
                """),
            {
                "video_id": str(video_id),
                "block_index": block.block_index,
                "start_ms": block.start_ms,
                "end_ms": block.end_ms,
                "speaker_label": block.speaker_label,
                "text": block.text,
                "segment_ids": json.dumps(block.segment_ids),
                "kind": block.kind,
                "formatter_version": block.formatter_version,
            },
        )


@_retry_on_transient_error
def list_transcript_blocks(db, video_id):
    try:
        return (
            db.execute(
                text("""
                    SELECT block_index, start_ms, end_ms, speaker_label, text,
                           segment_ids, kind, formatter_version
                    FROM transcript_blocks
                    WHERE video_id = :v
                    ORDER BY block_index
                    """),
                {"v": str(video_id)},
            )
            .mappings()
            .all()
        )
    except (OperationalError, ProgrammingError) as exc:
        if "transcript_blocks" in str(exc).lower():
            logger.warning(
                "transcript_blocks table is unavailable; falling back to derived formatted blocks",
                extra={"video_id": str(video_id), "error": str(exc)},
            )
            db.rollback()
            return []
        raise


@_retry_on_transient_error
def search_segments(db, q: str, video_id: str | None = None, limit: int = 50, offset: int = 0):
    from app.metrics import search_queries_total

    sql = """
        SELECT id, video_id, start_ms, end_ms,
               ts_headline(
                   'english', text, websearch_to_tsquery('english', :q), :headline_options
               ) AS snippet,
               ts_rank_cd(text_tsv, websearch_to_tsquery('english', :q)) AS rank
        FROM segments
        WHERE text_tsv @@ websearch_to_tsquery('english', :q)
        {video_filter}
        ORDER BY rank DESC, start_ms ASC
        LIMIT :limit OFFSET :offset
    """
    video_filter = ""
    params = {
        "q": q,
        "limit": limit,
        "offset": offset,
        "headline_options": POSTGRES_HEADLINE_OPTIONS,
    }
    if video_id:
        video_filter = "AND video_id = :vid"
        params["vid"] = str(video_id)
    rows = db.execute(text(sql.format(video_filter=video_filter)), params).mappings().all()

    # Track search query metric
    search_queries_total.labels(backend="postgres").inc()

    return normalize_search_rows(rows)


@_retry_on_transient_error
def search_youtube_segments(db, q: str, video_id: str | None = None, limit: int = 50, offset: int = 0):
    sql = """
        SELECT ys.id, yt.video_id, ys.start_ms, ys.end_ms,
               ts_headline(
                   'english', ys.text, websearch_to_tsquery('english', :q), :headline_options
               ) AS snippet,
               ts_rank_cd(ys.text_tsv, websearch_to_tsquery('english', :q)) AS rank
        FROM youtube_segments ys
        JOIN youtube_transcripts yt ON yt.id = ys.youtube_transcript_id
        WHERE ys.text_tsv @@ websearch_to_tsquery('english', :q)
        {video_filter}
        ORDER BY rank DESC, ys.start_ms ASC
        LIMIT :limit OFFSET :offset
    """
    video_filter = ""
    params = {
        "q": q,
        "limit": limit,
        "offset": offset,
        "headline_options": POSTGRES_HEADLINE_OPTIONS,
    }
    if video_id:
        video_filter = "AND yt.video_id = :vid"
        params["vid"] = str(video_id)
    rows = db.execute(text(sql.format(video_filter=video_filter)), params).mappings().all()
    return normalize_search_rows(rows)


@_retry_on_transient_error
def search_segments_advanced(
    db,
    q: str,
    video_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "relevance",
    filters: dict | None = None,
):
    """Search segments with advanced filters and sorting."""
    return _search_repository.search_native(
        db,
        q=q,
        video_id=video_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        filters=filters,
    )


@_retry_on_transient_error
def search_youtube_segments_advanced(
    db,
    q: str,
    video_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "relevance",
    filters: dict | None = None,
):
    """Search YouTube segments with advanced filters and sorting."""
    return _search_repository.search_youtube(
        db,
        q=q,
        video_id=video_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        filters=filters,
    )


@_retry_on_transient_error
def search_best_segments_advanced(
    db,
    q: str,
    video_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "relevance",
    filters: dict | None = None,
):
    """Search the best available transcript source per video.

    Policy: use Whisper/native segments when a video has them; otherwise use
    YouTube caption segments. This lets caption-only videos appear in default
    search without duplicating videos that already have Whisper transcripts.
    """
    return _search_repository.search_best(
        db,
        q=q,
        video_id=video_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        filters=filters,
    )
