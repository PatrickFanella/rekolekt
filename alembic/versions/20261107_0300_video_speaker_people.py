"""Attribute diarized speaker labels to archive people.

``video_speaker_people`` maps a recording's ``(video_id, speaker_label)`` to an
archive person, recording how (``manual`` or ``auto``), the match score, who
set it and when. A row with no person records that an administrator cleared
the label, so automatic matching does not fill it again.

``speaker_voice_references`` holds the enrolled voices: speaker embeddings
copied from ``video_speaker_centroids`` for labels an administrator confirmed.

Search documents gain ``speaker_person``, the slug of the published person a
segment's label maps to. Changing a segment's speaker label or deleting
segments flags that video's mappings as stale, because the labels no longer
mean what they meant when the mapping was made; the label trigger also
reindexes the video's segments through the outbox. The trigger function runs
as its owner so the least-privilege diarization role needs no new grants.
"""

from alembic import op

revision = "20261107_speaker_people"
down_revision = "20261107_segment_corrections"
branch_labels = None
depends_on = None

_ORIGINAL_NATIVE_ENQUEUE = """
    CREATE OR REPLACE FUNCTION enqueue_native_search_document() RETURNS trigger AS $$
    DECLARE record_value segments%ROWTYPE; op text;
    BEGIN
        record_value := CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
        op := CASE WHEN TG_OP='DELETE' THEN 'delete' ELSE 'upsert' END;
        INSERT INTO search_index_outbox(source, document_id, video_id, operation, version, payload)
        VALUES (
            'native', record_value.id, record_value.video_id, op,
            (extract(epoch FROM clock_timestamp()) * 1000000)::bigint,
            CASE WHEN op='delete' THEN NULL ELSE jsonb_build_object(
                'id', record_value.id, 'video_id', record_value.video_id,
                'start_ms', record_value.start_ms, 'end_ms', record_value.end_ms, 'text', record_value.text
            ) END
        );
        RETURN record_value;
    END; $$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute("""
        CREATE TABLE video_speaker_people (
            video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            speaker_label TEXT NOT NULL,
            person_id UUID REFERENCES archive_people(id) ON DELETE CASCADE,
            method TEXT NOT NULL,
            score DOUBLE PRECISION,
            set_by UUID REFERENCES users(id) ON DELETE SET NULL,
            set_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            invalidated_at TIMESTAMPTZ,
            invalidated_reason TEXT,
            PRIMARY KEY (video_id, speaker_label),
            CONSTRAINT video_speaker_people_method_check CHECK (method IN ('manual', 'auto')),
            CONSTRAINT video_speaker_people_auto_person_check CHECK (method = 'manual' OR person_id IS NOT NULL)
        );
        CREATE INDEX video_speaker_people_active_person_idx
            ON video_speaker_people (person_id) WHERE invalidated_at IS NULL;

        CREATE TABLE speaker_voice_references (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            person_id UUID NOT NULL REFERENCES archive_people(id) ON DELETE CASCADE,
            video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            speaker_label TEXT NOT NULL,
            segment_ids BIGINT[] NOT NULL,
            embedding REAL[] NOT NULL,
            model TEXT NOT NULL,
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT speaker_voice_references_unique UNIQUE (person_id, video_id, speaker_label),
            CONSTRAINT speaker_voice_references_segments_nonempty CHECK (cardinality(segment_ids) > 0)
        );
        CREATE INDEX speaker_voice_references_person_idx ON speaker_voice_references (person_id);

        CREATE FUNCTION native_search_payload(seg segments) RETURNS jsonb
        LANGUAGE sql STABLE AS $$
            SELECT jsonb_build_object(
                'id', seg.id, 'video_id', seg.video_id,
                'start_ms', seg.start_ms, 'end_ms', seg.end_ms, 'text', seg.text,
                'speaker_person', (
                    SELECT p.slug FROM video_speaker_people m
                    JOIN archive_people p ON p.id = m.person_id
                    WHERE m.video_id = seg.video_id AND m.speaker_label = seg.speaker_label
                      AND m.invalidated_at IS NULL AND p.status = 'published'
                )
            )
        $$;

        CREATE OR REPLACE FUNCTION enqueue_native_search_document() RETURNS trigger AS $$
        DECLARE record_value segments%ROWTYPE; op text;
        BEGIN
            record_value := CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
            op := CASE WHEN TG_OP='DELETE' THEN 'delete' ELSE 'upsert' END;
            INSERT INTO search_index_outbox(source, document_id, video_id, operation, version, payload)
            VALUES (
                'native', record_value.id, record_value.video_id, op,
                (extract(epoch FROM clock_timestamp()) * 1000000)::bigint,
                CASE WHEN op='delete' THEN NULL ELSE native_search_payload(record_value) END
            );
            RETURN record_value;
        END; $$ LANGUAGE plpgsql;

        CREATE FUNCTION invalidate_speaker_people_on_relabel() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        BEGIN
            IF NEW.speaker_label IS NOT DISTINCT FROM OLD.speaker_label THEN
                RETURN NULL;
            END IF;
            UPDATE video_speaker_people
            SET invalidated_at = now(), invalidated_reason = 'speaker labels changed'
            WHERE video_id = NEW.video_id AND invalidated_at IS NULL;
            IF FOUND THEN
                INSERT INTO search_index_outbox(source, document_id, video_id, operation, version, payload)
                SELECT 'native', s.id, s.video_id, 'upsert',
                       (extract(epoch FROM clock_timestamp()) * 1000000)::bigint, native_search_payload(s)
                FROM segments s WHERE s.video_id = NEW.video_id;
            END IF;
            RETURN NULL;
        END; $$;
        CREATE TRIGGER segments_speaker_relabel
        AFTER UPDATE OF speaker_label ON segments
        FOR EACH ROW EXECUTE FUNCTION invalidate_speaker_people_on_relabel();

        CREATE FUNCTION invalidate_speaker_people_on_segment_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE video_speaker_people
            SET invalidated_at = now(), invalidated_reason = 'transcript segments replaced'
            WHERE invalidated_at IS NULL AND video_id IN (SELECT DISTINCT video_id FROM deleted_segments);
            RETURN NULL;
        END; $$;
        CREATE TRIGGER segments_speaker_delete
        AFTER DELETE ON segments
        REFERENCING OLD TABLE AS deleted_segments
        FOR EACH STATEMENT EXECUTE FUNCTION invalidate_speaker_people_on_segment_delete();
        """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS segments_speaker_delete ON segments")
    op.execute("DROP FUNCTION IF EXISTS invalidate_speaker_people_on_segment_delete()")
    op.execute("DROP TRIGGER IF EXISTS segments_speaker_relabel ON segments")
    op.execute("DROP FUNCTION IF EXISTS invalidate_speaker_people_on_relabel()")
    op.execute(_ORIGINAL_NATIVE_ENQUEUE)
    op.execute("DROP FUNCTION IF EXISTS native_search_payload(segments)")
    op.execute("DROP TABLE speaker_voice_references")
    op.execute("DROP TABLE video_speaker_people")
