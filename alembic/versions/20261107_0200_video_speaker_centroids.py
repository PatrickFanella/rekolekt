"""Store one speaker embedding per diarized speaker of each recording.

The diarization worker writes a unit-length pyannote embedding (centroid) for
each ``Speaker N`` label it assigns. Speaker identification compares these
centroids with enrolled voices. Rows are replaced whenever a recording is
diarized again and removed with the video.
"""

from alembic import op

revision = "20261107_speaker_centroids"
down_revision = "20261108_chat_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE video_speaker_centroids (
            video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            speaker_label TEXT NOT NULL,
            embedding REAL[] NOT NULL,
            speech_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
            model TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (video_id, speaker_label),
            CONSTRAINT video_speaker_centroids_embedding_nonempty CHECK (cardinality(embedding) > 0)
        )
        """)


def downgrade() -> None:
    op.execute("DROP TABLE video_speaker_centroids")
