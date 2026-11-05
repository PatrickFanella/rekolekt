"""Store each video's broadcast date separately from its upload date.

Adds ``videos.broadcast_date``, ``broadcast_date_source`` and
``broadcast_kind``, backfills them from titles and upload dates, adds a
``coverage`` snapshot to named period stats, and repoints topic mention dates
at the broadcast date.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261105_broadcast_dates"
down_revision = "20261008_search_freshness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("broadcast_date", sa.Date(), nullable=True))
    op.add_column("videos", sa.Column("broadcast_date_source", sa.Text(), nullable=True))
    op.add_column("videos", sa.Column("broadcast_kind", sa.Text(), nullable=False, server_default="vod"))
    op.create_check_constraint(
        "videos_broadcast_date_source_check",
        "videos",
        "broadcast_date_source IS NULL OR broadcast_date_source IN ('title', 'upload_date', 'manual')",
    )
    op.create_check_constraint(
        "videos_broadcast_kind_check", "videos", "broadcast_kind IN ('vod', 'no_stream', 'extra')"
    )
    op.create_index("videos_broadcast_date_idx", "videos", ["broadcast_date"])
    op.add_column(
        "archive_named_period_stats",
        sa.Column("coverage", sa.dialects.postgresql.JSONB(), nullable=True),
    )

    # Imported lazily: the derivation is pure Python with no app start-up cost.
    from app.archive.broadcast_dates import refresh_broadcast_dates

    refresh_broadcast_dates(op.get_bind())
    op.execute("""
        UPDATE archive_topic_mentions m
        SET occurred_at = v.broadcast_date::timestamp AT TIME ZONE 'UTC'
        FROM videos v
        WHERE v.id = m.video_id
          AND m.occurred_at IS DISTINCT FROM v.broadcast_date::timestamp AT TIME ZONE 'UTC'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE archive_topic_mentions m
        SET occurred_at = v.uploaded_at
        FROM videos v
        WHERE v.id = m.video_id
    """)
    op.drop_column("archive_named_period_stats", "coverage")
    op.drop_index("videos_broadcast_date_idx", table_name="videos")
    op.drop_constraint("videos_broadcast_kind_check", "videos", type_="check")
    op.drop_constraint("videos_broadcast_date_source_check", "videos", type_="check")
    op.drop_column("videos", "broadcast_kind")
    op.drop_column("videos", "broadcast_date_source")
    op.drop_column("videos", "broadcast_date")
