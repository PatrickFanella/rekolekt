"""Incremental discovery of uploads from configured YouTube channels."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

from sqlalchemy import text

from app.archive.broadcast_dates import broadcast_columns
from app.channel_sync_rules import SyncChannel, entry_published_date
from app.channel_sync_rules import parse_channel_urls as parse_channel_urls
from app.logging_config import get_logger
from worker.youtube.service import get_youtube_service

logger = get_logger(__name__)


@dataclass(frozen=True)
class ChannelVideo:
    youtube_id: str
    title: str
    duration_seconds: int | None
    channel_name: str | None


@dataclass(frozen=True)
class ChannelSyncResult:
    discovered: int = 0
    enqueued: int = 0
    failed_channels: int = 0
    skipped: int = 0


class ChannelSyncRepository(Protocol):
    def enqueue_missing(self, video: ChannelVideo) -> bool: ...


class SqlChannelSyncRepository:
    """Persist channel discoveries as independent, captions-first system jobs."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def enqueue_missing(self, video: ChannelVideo) -> bool:
        input_url = f"https://www.youtube.com/watch?v={video.youtube_id}"
        row = self.conn.execute(
            text("""
                WITH channel_lock AS MATERIALIZED (
                    SELECT pg_advisory_xact_lock(hashtextextended(:youtube_id, 0))
                ), missing_video AS (
                    SELECT CAST(:youtube_id AS text) AS youtube_id FROM channel_lock
                    WHERE NOT EXISTS (
                        SELECT 1 FROM videos WHERE videos.youtube_id = :youtube_id
                    )
                ), new_job AS (
                    INSERT INTO jobs (
                        kind, input_url, state, stage, meta, canonical_source, idempotency_key
                    )
                    SELECT
                        'single', :input_url, 'downloading', 'queued', CAST(:meta AS jsonb),
                        'youtube:video:' || youtube_id, 'channel-sync:' || youtube_id
                    FROM missing_video
                    RETURNING id
                )
                INSERT INTO videos (
                    job_id, youtube_id, idx, title, duration_seconds, channel_name,
                    broadcast_date, broadcast_date_source, broadcast_kind
                )
                SELECT
                    id, :youtube_id, 0, :title, :duration_seconds, :channel_name,
                    :broadcast_date, :broadcast_date_source, :broadcast_kind
                FROM new_job
                ON CONFLICT (job_id, youtube_id) DO NOTHING
                RETURNING id
            """),
            {
                "youtube_id": video.youtube_id,
                "input_url": input_url,
                "title": video.title,
                "duration_seconds": video.duration_seconds,
                "channel_name": video.channel_name,
                "meta": json.dumps({"created_by": "channel_sync", "staged": True}),
                **broadcast_columns(video.title, None),
            },
        ).first()
        return row is not None


def sync_configured_channels(conn: Any, channels: Iterable[SyncChannel]) -> ChannelSyncResult:
    configured = tuple(channels)
    service = get_youtube_service()
    result = ChannelSyncService(
        repository=SqlChannelSyncRepository(conn),
        fetch_channel=lambda url: service.fetch_metadata(url, flat_playlist=True),
    ).sync(configured)
    logger.info(
        "Recurring channel sync complete",
        extra={
            "channel_count": len(configured),
            "discovered": result.discovered,
            "enqueued": result.enqueued,
            "skipped": result.skipped,
            "failed_channels": result.failed_channels,
        },
    )
    return result


class ChannelSyncService:
    """Discover channel uploads and enqueue each globally missing video once.

    Each channel's inclusion rules run before ``enqueue_missing``, so a skipped
    upload never creates a job. Videos already in the archive are not touched.
    """

    def __init__(
        self,
        *,
        repository: ChannelSyncRepository,
        fetch_channel: Callable[[str], dict[str, Any]],
    ) -> None:
        self.repository = repository
        self.fetch_channel = fetch_channel

    def sync(self, channels: Iterable[SyncChannel | str]) -> ChannelSyncResult:
        discovered = 0
        enqueued = 0
        failed_channels = 0
        skipped_total = 0
        for channel in channels:
            if isinstance(channel, str):
                channel = SyncChannel(url=channel)
            channel_url = channel.url
            try:
                metadata = self.fetch_channel(channel_url)
            except Exception as exc:
                failed_channels += 1
                logger.warning(
                    "Recurring channel discovery failed",
                    extra={"channel_url": channel_url, "error": str(exc)[:200]},
                )
                continue
            channel_name = metadata.get("channel") or metadata.get("uploader") or metadata.get("title")
            skipped: Counter[str] = Counter()
            for entry in metadata.get("entries") or ():
                youtube_id = entry.get("id")
                if not youtube_id:
                    continue
                discovered += 1
                reason = channel.rules.skip_reason(
                    title=str(entry.get("title") or ""),
                    duration_seconds=_duration_seconds(entry.get("duration")),
                    published=entry_published_date(entry),
                )
                if reason is not None:
                    skipped[reason] += 1
                    continue
                video = ChannelVideo(
                    youtube_id=str(youtube_id),
                    title=str(entry.get("title") or ""),
                    duration_seconds=entry.get("duration"),
                    channel_name=entry.get("channel") or entry.get("uploader") or channel_name,
                )
                if self.repository.enqueue_missing(video):
                    enqueued += 1
            if skipped:
                skipped_total += sum(skipped.values())
                logger.info(
                    "Recurring channel uploads skipped by rule",
                    extra={"channel_url": channel_url, "skipped_by_rule": dict(sorted(skipped.items()))},
                )
        return ChannelSyncResult(
            discovered=discovered,
            enqueued=enqueued,
            failed_channels=failed_channels,
            skipped=skipped_total,
        )


def _duration_seconds(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


class ChannelSyncScheduler:
    """Run a channel sync immediately and then at a bounded interval."""

    MAX_INTERVAL_SECONDS = 24 * 60 * 60

    def __init__(self, *, interval_seconds: int, sync: Callable[[], object]) -> None:
        if interval_seconds <= 0 or interval_seconds > self.MAX_INTERVAL_SECONDS:
            raise ValueError("channel sync interval must be between 1 second and 24 hours")
        self.interval_seconds = interval_seconds
        self.sync = sync
        self.next_run_at = 0.0

    def run_if_due(self, *, now: float) -> bool:
        if now < self.next_run_at:
            return False
        self.next_run_at = now + self.interval_seconds
        self.sync()
        return True
