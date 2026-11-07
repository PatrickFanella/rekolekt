"""Reviewed transcript corrections proposed by signed-in users.

Corrections are keyed by video, transcript source, segment time range and a
digest of the machine text, not by segment row id, because re-transcription
deletes and rewrites segment rows. ``accepted_at`` marks rows a moderator has
accepted; only those are re-applied (or marked stale) after reprocessing.
"""

from alembic import op

revision = "20261107_segment_corrections"
down_revision = "20261107_speaker_centroids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE segment_corrections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('whisper','youtube')),
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms >= start_ms),
    original_sha256 TEXT NOT NULL CHECK (original_sha256 ~ '^[0-9a-f]{64}$'),
    original_text TEXT NOT NULL,
    corrected_text TEXT NOT NULL CHECK (length(btrim(corrected_text)) BETWEEN 1 AND 2000),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','accepted','rejected','stale','superseded')),
    proposer_id UUID REFERENCES users(id) ON DELETE SET NULL,
    reviewer_id UUID REFERENCES users(id) ON DELETE SET NULL,
    review_reason TEXT CHECK (review_reason IS NULL OR length(btrim(review_reason)) BETWEEN 1 AND 1000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status <> 'accepted' OR accepted_at IS NOT NULL)
);
CREATE UNIQUE INDEX segment_corrections_one_applied
    ON segment_corrections(video_id, source, start_ms, end_ms) WHERE status = 'accepted';
CREATE INDEX segment_corrections_by_video ON segment_corrections(video_id, source, start_ms);
CREATE INDEX segment_corrections_queue ON segment_corrections(status, created_at)
    WHERE status IN ('pending','stale');
CREATE INDEX segment_corrections_proposer ON segment_corrections(proposer_id, created_at DESC);
    """)


def downgrade() -> None:
    # Put the machine text back where an accepted correction is still applied.
    op.execute("""
UPDATE segments s SET text = c.original_text FROM segment_corrections c
WHERE c.status = 'accepted' AND c.source = 'whisper' AND s.video_id = c.video_id
  AND s.start_ms = c.start_ms AND s.end_ms = c.end_ms AND s.text = c.corrected_text;
UPDATE youtube_segments s SET text = c.original_text FROM segment_corrections c, youtube_transcripts t
WHERE c.status = 'accepted' AND c.source = 'youtube' AND t.id = s.youtube_transcript_id AND t.video_id = c.video_id
  AND s.start_ms = c.start_ms AND s.end_ms = c.end_ms AND s.text = c.corrected_text;
DROP TABLE segment_corrections;
    """)
