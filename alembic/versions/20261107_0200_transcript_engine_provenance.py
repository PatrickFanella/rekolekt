"""Record the transcription engine that produced each native transcript.

``transcripts.model`` held the configured ``WHISPER_MODEL`` even when the worker
fell back to a smaller model. These columns hold what actually ran. Rows created
before this migration keep NULL and are reported as unknown; nothing is guessed.
"""

from alembic import op

revision = "20261107_transcript_engine"
down_revision = "20261107_caption_overshadowed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE transcripts
            ADD COLUMN engine_model TEXT,
            ADD COLUMN engine_backend TEXT,
            ADD COLUMN engine_device TEXT,
            ADD COLUMN engine_compute_type TEXT,
            ADD COLUMN engine_version TEXT
        """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE transcripts
            DROP COLUMN engine_model,
            DROP COLUMN engine_backend,
            DROP COLUMN engine_device,
            DROP COLUMN engine_compute_type,
            DROP COLUMN engine_version
        """)
