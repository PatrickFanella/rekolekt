"""Flag captions that a native transcript overshadows.

Best-source search shows YouTube captions only for videos without a native
transcript, but half of all caption lines belong to transcribed videos. The
caption full-text indexes returned those lines too, and search then read them
from the heap only to throw them away (about 2.3 s for a common word).

``overshadowed`` marks those transcripts and their lines, and partial indexes
cover only the captions search can still show. Triggers keep the flag in step
with native segments as videos are transcribed, reprocessed or deleted.

The backfill rewrites about half of ``youtube_segments`` and the table is then
compacted with ``VACUUM FULL``, which locks it exclusively; run this migration
in a maintenance window.
"""

from alembic import op

revision = "20261107_caption_overshadowed"
down_revision = "20261106_segment_words"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE youtube_transcripts ADD COLUMN overshadowed BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE youtube_segments ADD COLUMN overshadowed BOOLEAN NOT NULL DEFAULT false")

    # New caption tracks and lines take the current state of their video.
    op.execute("""
        CREATE FUNCTION youtube_transcripts_set_overshadowed() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.overshadowed := EXISTS (SELECT 1 FROM segments s WHERE s.video_id = NEW.video_id);
            RETURN NEW;
        END
        $$
        """)
    op.execute("""
        CREATE TRIGGER youtube_transcripts_overshadowed
        BEFORE INSERT ON youtube_transcripts
        FOR EACH ROW EXECUTE FUNCTION youtube_transcripts_set_overshadowed()
        """)
    op.execute("""
        CREATE FUNCTION youtube_segments_set_overshadowed() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.overshadowed := COALESCE(
                (SELECT yt.overshadowed FROM youtube_transcripts yt WHERE yt.id = NEW.youtube_transcript_id),
                false
            );
            RETURN NEW;
        END
        $$
        """)
    op.execute("""
        CREATE TRIGGER youtube_segments_overshadowed
        BEFORE INSERT ON youtube_segments
        FOR EACH ROW EXECUTE FUNCTION youtube_segments_set_overshadowed()
        """)

    # Native segments arrive one INSERT at a time, so the transcript flag is
    # the cheap guard: only the first segment of a video touches caption lines.
    op.execute("""
        CREATE FUNCTION segments_overshadow_captions() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            WITH flagged AS (
                UPDATE youtube_transcripts yt SET overshadowed = true
                WHERE yt.video_id IN (SELECT DISTINCT video_id FROM inserted_segments)
                  AND NOT yt.overshadowed
                RETURNING yt.id
            )
            UPDATE youtube_segments ys SET overshadowed = true
            FROM flagged WHERE ys.youtube_transcript_id = flagged.id;
            RETURN NULL;
        END
        $$
        """)
    op.execute("""
        CREATE TRIGGER segments_overshadow_captions
        AFTER INSERT ON segments
        REFERENCING NEW TABLE AS inserted_segments
        FOR EACH STATEMENT EXECUTE FUNCTION segments_overshadow_captions()
        """)
    op.execute("""
        CREATE FUNCTION segments_release_captions() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            WITH released AS (
                UPDATE youtube_transcripts yt SET overshadowed = false
                WHERE yt.video_id IN (SELECT DISTINCT video_id FROM deleted_segments)
                  AND yt.overshadowed
                  AND NOT EXISTS (SELECT 1 FROM segments s WHERE s.video_id = yt.video_id)
                RETURNING yt.id
            )
            UPDATE youtube_segments ys SET overshadowed = false
            FROM released WHERE ys.youtube_transcript_id = released.id;
            RETURN NULL;
        END
        $$
        """)
    op.execute("""
        CREATE TRIGGER segments_release_captions
        AFTER DELETE ON segments
        REFERENCING OLD TABLE AS deleted_segments
        FOR EACH STATEMENT EXECUTE FUNCTION segments_release_captions()
        """)

    op.execute("""
        UPDATE youtube_transcripts yt SET overshadowed = true
        WHERE EXISTS (SELECT 1 FROM segments s WHERE s.video_id = yt.video_id)
        """)
    op.execute("""
        UPDATE youtube_segments ys SET overshadowed = true
        FROM youtube_transcripts yt
        WHERE yt.id = ys.youtube_transcript_id AND yt.overshadowed
        """)

    with op.get_context().autocommit_block():
        # The backfill left a dead copy of every flagged line; compact before
        # building the new indexes so they are built once, on the final heap.
        op.execute("VACUUM (FULL, ANALYZE) youtube_segments")
        op.execute("VACUUM ANALYZE youtube_transcripts")
    op.execute("""
        CREATE INDEX youtube_segments_caption_only_tsv_idx
        ON youtube_segments USING GIN (text_tsv) WHERE NOT overshadowed
        """)
    op.execute("""
        CREATE INDEX youtube_segments_caption_only_simple_tsv_idx
        ON youtube_segments USING GIN (to_tsvector('simple', COALESCE(text, ''))) WHERE NOT overshadowed
        """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS youtube_segments_caption_only_simple_tsv_idx")
    op.execute("DROP INDEX IF EXISTS youtube_segments_caption_only_tsv_idx")
    op.execute("DROP TRIGGER IF EXISTS segments_release_captions ON segments")
    op.execute("DROP TRIGGER IF EXISTS segments_overshadow_captions ON segments")
    op.execute("DROP TRIGGER IF EXISTS youtube_segments_overshadowed ON youtube_segments")
    op.execute("DROP TRIGGER IF EXISTS youtube_transcripts_overshadowed ON youtube_transcripts")
    op.execute("DROP FUNCTION IF EXISTS segments_release_captions()")
    op.execute("DROP FUNCTION IF EXISTS segments_overshadow_captions()")
    op.execute("DROP FUNCTION IF EXISTS youtube_segments_set_overshadowed()")
    op.execute("DROP FUNCTION IF EXISTS youtube_transcripts_set_overshadowed()")
    op.execute("ALTER TABLE youtube_segments DROP COLUMN overshadowed")
    op.execute("ALTER TABLE youtube_transcripts DROP COLUMN overshadowed")
