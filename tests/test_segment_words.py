"""Word timings live beside segments and follow them on delete."""

import json
import uuid

from sqlalchemy import text


def test_segment_words_follow_their_segment(db_session):
    db = db_session
    job_id, video_id = uuid.uuid4(), uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO jobs (id, kind, input_url, state) VALUES (:id, 'single', 'https://example.test', 'completed')"
        ),
        {"id": str(job_id)},
    )
    db.execute(
        text(
            "INSERT INTO videos (id, job_id, youtube_id, idx, title, state) VALUES (:id, :job, :yt, 0, 't', 'completed')"
        ),
        {"id": str(video_id), "job": str(job_id), "yt": video_id.hex[:11]},
    )
    segment_id = db.execute(
        text("INSERT INTO segments (video_id, start_ms, end_ms, text) VALUES (:v, 0, 900, 'hi') RETURNING id"),
        {"v": str(video_id)},
    ).scalar_one()
    words = [{"word": "hi", "start": 0.0, "end": 0.9}]
    db.execute(
        text("INSERT INTO segment_words (segment_id, words) VALUES (:id, CAST(:w AS jsonb))"),
        {"id": segment_id, "w": json.dumps(words)},
    )

    stored = db.execute(text("SELECT words FROM segment_words WHERE segment_id = :id"), {"id": segment_id}).scalar_one()
    assert stored == words
    columns = db.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'segments'")
    ).scalars()
    assert "word_timestamps" not in set(columns)

    db.execute(text("DELETE FROM segments WHERE video_id = :v"), {"v": str(video_id)})
    assert (
        db.execute(text("SELECT count(*) FROM segment_words WHERE segment_id = :id"), {"id": segment_id}).scalar_one()
        == 0
    )
