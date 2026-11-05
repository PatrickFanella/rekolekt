"""Citation-backed product intelligence assembled from canonical PostgreSQL search."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Literal

from sqlalchemy import text

from .. import crud
from ..schemas import (
    ArchiveEvidenceMoment,
    QuotedMoment,
    QuotedMomentsResponse,
    RelatedEpisode,
    RelatedEpisodesResponse,
    TopicTimelineBucket,
    TopicTimelineResponse,
)
from .intelligence_queries.periods import bucket_period_and_label

TOPIC_TIMELINE_PAGE_SIZE = 500
TOPIC_TIMELINE_MAX_PAGES = 20


def _bucket_key(value: date, granularity: Literal["week", "month"]) -> tuple[str, str]:
    return bucket_period_and_label(value, granularity)


def build_topic_timeline(
    db,
    *,
    slug: str,
    granularity: Literal["week", "month"],
    date_from: date | None,
    date_to: date | None,
) -> TopicTimelineResponse:
    """Bucket a topic's mentions by broadcast date across every matching video.

    Pages through grouped search so counts are not limited to the first page
    of results, bounded at TOPIC_TIMELINE_MAX_PAGES pages.
    """

    query = slug.replace("-", " ").strip()
    filters = {
        **({"date_from": date_from.isoformat()} if date_from else {}),
        **({"date_to": date_to.isoformat()} if date_to else {}),
    }
    buckets: dict[str, dict] = defaultdict(lambda: {"label": "", "moments": [], "videos": set()})
    for page_number in range(TOPIC_TIMELINE_MAX_PAGES):
        grouped = crud.get_grouped_search(
            db,
            q=query,
            source="best",
            limit=TOPIC_TIMELINE_PAGE_SIZE,
            offset=page_number * TOPIC_TIMELINE_PAGE_SIZE,
            filters=filters,
        )
        for group in grouped.groups:
            broadcast_date = group.video.broadcast_date
            if broadcast_date is None:
                continue
            key, label = _bucket_key(broadcast_date, granularity)
            bucket = buckets[key]
            bucket["label"] = label
            bucket["videos"].add(str(group.video.id))
            for moment in group.moments:
                bucket["moments"].append(
                    ArchiveEvidenceMoment(
                        video=group.video,
                        start_ms=moment.start_ms,
                        end_ms=moment.end_ms,
                        snippet=moment.snippet,
                        topic=query,
                    )
                )
        if grouped.total_moments < TOPIC_TIMELINE_PAGE_SIZE:
            break
    return TopicTimelineResponse(
        topic=query,
        granularity=granularity,
        date_from=date_from,
        date_to=date_to,
        buckets=[
            TopicTimelineBucket(
                period=key,
                label=value["label"],
                mention_count=len(value["moments"]),
                episode_count=len(value["videos"]),
                evidence=value["moments"][:5],
            )
            for key, value in sorted(buckets.items())
        ],
    )


def related_episodes(db, *, video_id, limit: int = 8) -> RelatedEpisodesResponse:
    rows = (
        db.execute(
            text("""
            WITH source_features AS (
                SELECT 'tag:' || t.slug AS feature, 'Shared tag: ' || t.label AS reason
                FROM archive_video_taggings vt JOIN archive_video_tags t ON t.id=vt.tag_id
                WHERE vt.video_id=:video_id AND t.status='published'
                UNION ALL
                SELECT 'person:' || p.slug, 'Shared person: ' || p.display_name
                FROM archive_video_people vp JOIN archive_people p ON p.id=vp.person_id
                WHERE vp.video_id=:video_id AND p.status='published'
            ), candidate_features AS (
                SELECT vt.video_id, 'tag:' || t.slug AS feature
                FROM archive_video_taggings vt JOIN archive_video_tags t ON t.id=vt.tag_id
                WHERE vt.video_id<>:video_id AND t.status='published'
                UNION ALL
                SELECT vp.video_id, 'person:' || p.slug
                FROM archive_video_people vp JOIN archive_people p ON p.id=vp.person_id
                WHERE vp.video_id<>:video_id AND p.status='published'
            )
            SELECT cf.video_id, COUNT(*)::float AS score, array_agg(sf.reason ORDER BY sf.reason) AS reasons
            FROM candidate_features cf JOIN source_features sf USING(feature)
            GROUP BY cf.video_id ORDER BY score DESC, cf.video_id LIMIT :limit
        """),
            {"video_id": video_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    videos = {str(video.id): video for video in crud.get_videos_by_ids(db, [str(row["video_id"]) for row in rows])}
    return RelatedEpisodesResponse(
        items=[
            RelatedEpisode(video=videos[str(row["video_id"])], score=float(row["score"]), reasons=list(row["reasons"]))
            for row in rows
            if str(row["video_id"]) in videos
        ]
    )


def quoted_moments(db, *, video_id, limit: int = 10) -> QuotedMomentsResponse:
    rows = (
        db.execute(
            text("""
            SELECT start_ms,end_ms,COALESCE(NULLIF(text,''),'Saved transcript moment') AS snippet,
                   COUNT(*)::int AS quote_count
            FROM favorites WHERE video_id=:video_id
            GROUP BY start_ms,end_ms,COALESCE(NULLIF(text,''),'Saved transcript moment')
            ORDER BY quote_count DESC,start_ms ASC LIMIT :limit
        """),
            {"video_id": video_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    return QuotedMomentsResponse(
        video_id=video_id,
        items=[QuotedMoment(**dict(row)) for row in rows],
    )
