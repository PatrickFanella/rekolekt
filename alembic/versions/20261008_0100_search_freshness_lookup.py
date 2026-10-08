"""Index the oldest pending document for request-time freshness metadata."""

from alembic import op

revision = "20261008_search_freshness"
down_revision = "20260919_community"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS search_index_outbox_oldest_pending_idx
            ON search_index_outbox (created_at)
            WHERE processed_at IS NULL AND dead_lettered_at IS NULL
        """)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS search_index_outbox_oldest_pending_idx")
