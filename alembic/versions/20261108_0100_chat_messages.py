"""Store an imported chat log per recording, aligned to its timeline.

``chat_imports`` holds one import per video: the log's format, file name and
SHA-256 digest, how it was aligned (``manual`` offset or ``audio``
cross-correlation), the piecewise mapping from log time to recording time and
the alignment confidence. ``chat_messages`` holds the aligned messages.
Deleting the video removes both; re-importing replaces the import row and
its messages in one transaction.
"""

from alembic import op

revision = "20261108_chat_messages"
down_revision = "20261107_transcript_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE chat_imports (
            video_id UUID PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
            source_format TEXT NOT NULL CHECK (source_format IN ('twitchdownloader', 'rustlog')),
            source_name TEXT NOT NULL CHECK (length(source_name) BETWEEN 1 AND 255),
            source_digest TEXT NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
            broadcast_started_at TIMESTAMPTZ,
            alignment_method TEXT NOT NULL CHECK (alignment_method IN ('manual', 'audio')),
            alignment_mapping JSONB NOT NULL CHECK (jsonb_typeof(alignment_mapping) = 'array'),
            alignment_confidence DOUBLE PRECISION CHECK (alignment_confidence BETWEEN 0 AND 1),
            message_count INTEGER NOT NULL CHECK (message_count >= 0),
            imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK ((alignment_method = 'manual') = (alignment_confidence IS NULL))
        )
        """)
    op.execute("""
        CREATE TABLE chat_messages (
            id BIGSERIAL PRIMARY KEY,
            video_id UUID NOT NULL REFERENCES chat_imports(video_id) ON DELETE CASCADE,
            offset_ms INTEGER NOT NULL CHECK (offset_ms >= 0),
            author TEXT NOT NULL CHECK (length(author) BETWEEN 1 AND 64),
            body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 1000),
            metadata JSONB
        )
        """)
    op.execute("CREATE INDEX chat_messages_video_offset ON chat_messages (video_id, offset_ms, id)")


def downgrade() -> None:
    op.execute("DROP TABLE chat_messages")
    op.execute("DROP TABLE chat_imports")
