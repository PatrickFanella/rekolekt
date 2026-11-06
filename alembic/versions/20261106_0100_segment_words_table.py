"""Move word-level timings out of the segments heap.

``segments.word_timestamps`` averaged about 880 bytes per row and made up most
of the table, so every full-text search read roughly four times more heap than
it needed. The timings move to ``segment_words`` keyed by segment id, and the
segments table is rewritten to reclaim the space.

The rewrite (``VACUUM FULL``) takes an exclusive lock on ``segments`` for the
length of the copy; run this migration in a maintenance window.
"""

from alembic import op

revision = "20261106_segment_words"
down_revision = "20261105_broadcast_dates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE segment_words (
            segment_id BIGINT PRIMARY KEY REFERENCES segments(id) ON DELETE CASCADE,
            words JSONB NOT NULL
        )
        """)
    op.execute("""
        INSERT INTO segment_words (segment_id, words)
        SELECT id, word_timestamps FROM segments WHERE word_timestamps IS NOT NULL
        """)
    op.execute("ALTER TABLE segments DROP COLUMN word_timestamps")
    # Dropping a column leaves its bytes in place; rewrite the table to shrink it.
    with op.get_context().autocommit_block():
        op.execute("VACUUM (FULL, ANALYZE) segments")


def downgrade() -> None:
    op.execute("ALTER TABLE segments ADD COLUMN word_timestamps JSONB")
    op.execute("""
        UPDATE segments s SET word_timestamps = w.words
        FROM segment_words w WHERE w.segment_id = s.id
        """)
    op.execute("DROP TABLE segment_words")
