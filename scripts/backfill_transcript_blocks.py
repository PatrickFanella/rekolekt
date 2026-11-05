#!/usr/bin/env python3
"""Backfill persisted transcript blocks for existing videos.

Finds videos that have raw segments but no transcript_blocks rows, whose
stored blocks were built by another FORMATTER_VERSION, or whose diarization
completed after the blocks were stored, rebuilds the blocks
deterministically from segments, and persists them.

Videos are visited in id order (keyset pagination), each one in its own short
transaction, so the run never holds a long transaction and can be repeated or
resumed safely: rebuilt videos no longer match the selection.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

# Ensure repo root is on sys.path even if run from outside the repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import crud
from app.logging_config import configure_logging, get_logger
from app.settings import settings
from app.transcripts.blocks import FORMATTER_VERSION, build_transcript_blocks
from app.transcripts.types import TranscriptSegment

configure_logging(
    service="script.backfill-transcript-blocks",
    level=settings.LOG_LEVEL,
    json_format=(settings.LOG_FORMAT == "json"),
)
logger = get_logger(__name__)


def _load_batch(conn, limit: int, after_id: str):
    return (
        conn.execute(
            text("""
            SELECT v.id, v.youtube_id, v.title
            FROM videos v
            WHERE v.id > CAST(:after_id AS uuid)
              AND EXISTS (SELECT 1 FROM segments s WHERE s.video_id = v.id)
              AND (
                  NOT EXISTS (SELECT 1 FROM transcript_blocks tb WHERE tb.video_id = v.id)
                  OR EXISTS (
                      SELECT 1 FROM transcript_blocks tb
                      WHERE tb.video_id = v.id AND tb.formatter_version <> :formatter_version
                  )
                  -- Diarization updates speaker labels after the blocks were stored, but its
                  -- database role may not write transcript_blocks. The native pipeline writes
                  -- blocks and videos.updated_at in one transaction (equal now()), so only a
                  -- later video update such as diarization completion matches here.
                  OR (
                      v.diarization_state = 'completed'
                      AND EXISTS (
                          SELECT 1 FROM transcript_blocks tb
                          WHERE tb.video_id = v.id AND tb.created_at < v.updated_at
                      )
                  )
              )
            ORDER BY v.id
            LIMIT :limit
            """),
            {"limit": limit, "after_id": after_id, "formatter_version": FORMATTER_VERSION},
        )
        .mappings()
        .all()
    )


def _load_segments(conn, video_id):
    rows = (
        conn.execute(
            text("""
            SELECT id, start_ms, end_ms, text, speaker_label
            FROM segments
            WHERE video_id = :video_id
            ORDER BY start_ms, id
            """),
            {"video_id": str(video_id)},
        )
        .mappings()
        .all()
    )

    return [
        TranscriptSegment(
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            text=row["text"],
            speaker_label=row["speaker_label"],
        )
        for row in rows
    ]


_FIRST_UUID = "00000000-0000-0000-0000-000000000000"


def main(batch_size: int = 100, dry_run: bool = False, until_empty: bool = True, engine=None) -> int:
    engine = engine or create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    processed = 0
    after_id = _FIRST_UUID

    logger.info(
        "Starting transcript block backfill",
        extra={"batch_size": batch_size, "dry_run": dry_run, "formatter_version": FORMATTER_VERSION},
    )

    while True:
        with engine.begin() as conn:
            batch = _load_batch(conn, batch_size, after_id)

        if not batch:
            logger.info("No videos eligible for transcript block backfill")
            break

        logger.info("Loaded batch", extra={"batch_size": len(batch)})

        for row in batch:
            video_id = row["id"]
            after_id = str(video_id)
            # One short transaction per video: read segments and swap its blocks atomically.
            with engine.begin() as conn:
                segments = _load_segments(conn, video_id)
                if not segments:
                    logger.info("Skipping video with no segments", extra={"video_id": str(video_id)})
                    continue

                blocks = build_transcript_blocks(segments)
                if not dry_run:
                    crud.replace_transcript_blocks(conn, video_id, blocks)
            processed += 1
            logger.info(
                "Built transcript blocks",
                extra={
                    "video_id": str(video_id),
                    "youtube_id": row["youtube_id"],
                    "title": row["title"],
                    "segment_count": len(segments),
                    "block_count": len(blocks),
                    "dry_run": dry_run,
                },
            )

        if not until_empty or len(batch) < batch_size:
            break

    logger.info("Transcript block backfill complete", extra={"videos_processed": processed, "dry_run": dry_run})
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build missing or outdated persisted transcript blocks")
    parser.add_argument("--batch-size", type=int, default=100, help="Videos to process per batch (default: 100)")
    parser.add_argument("--dry-run", action="store_true", help="Build blocks without writing to the database")
    parser.add_argument("--once", action="store_true", help="Run a single batch instead of looping until empty")
    args = parser.parse_args()

    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be a positive integer")

    raise SystemExit(main(batch_size=args.batch_size, dry_run=args.dry_run, until_empty=not args.once))
