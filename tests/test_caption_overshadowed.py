"""Captions are flagged while a native transcript exists for their video."""

import uuid

from sqlalchemy import text


def _video(db):
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
    return str(video_id)


def _captions(db, video_id, lines):
    transcript_id = db.execute(
        text("INSERT INTO youtube_transcripts (video_id, language) VALUES (:v, 'en') RETURNING id"), {"v": video_id}
    ).scalar_one()
    for start, line in enumerate(lines):
        _caption_line(db, transcript_id, start, line)
    return transcript_id


def _caption_line(db, transcript_id, start, line):
    db.execute(
        text(
            "INSERT INTO youtube_segments (youtube_transcript_id, start_ms, end_ms, text) VALUES (:t, :s, :s + 1, :x)"
        ),
        {"t": transcript_id, "s": start * 1000, "x": line},
    )


def _flags(db, transcript_id):
    transcript = db.execute(
        text("SELECT overshadowed FROM youtube_transcripts WHERE id = :t"), {"t": transcript_id}
    ).scalar_one()
    lines = set(
        db.execute(
            text("SELECT overshadowed FROM youtube_segments WHERE youtube_transcript_id = :t"), {"t": transcript_id}
        ).scalars()
    )
    return transcript, lines


def _native_segment(db, video_id, start=0):
    db.execute(
        text("INSERT INTO segments (video_id, start_ms, end_ms, text) VALUES (:v, :s, :s + 900, 'native')"),
        {"v": video_id, "s": start},
    )


def test_native_segments_flag_and_release_captions(db_session):
    db = db_session
    video_id = _video(db)
    transcript_id = _captions(db, video_id, ["one", "two"])
    assert _flags(db, transcript_id) == (False, {False})

    _native_segment(db, video_id, 0)
    _native_segment(db, video_id, 1000)
    assert _flags(db, transcript_id) == (True, {True})

    _caption_line(db, transcript_id, 5, "late line")
    assert _flags(db, transcript_id) == (True, {True})

    db.execute(text("DELETE FROM segments WHERE video_id = :v AND start_ms = 0"), {"v": video_id})
    assert _flags(db, transcript_id) == (True, {True})
    db.execute(text("DELETE FROM segments WHERE video_id = :v"), {"v": video_id})
    assert _flags(db, transcript_id) == (False, {False})


def test_captions_ingested_after_transcription_start_flagged(db_session):
    db = db_session
    video_id, other_id = _video(db), _video(db)
    _native_segment(db, video_id)
    transcript_id = _captions(db, video_id, ["one"])
    other_transcript = _captions(db, other_id, ["two"])
    assert _flags(db, transcript_id) == (True, {True})
    assert _flags(db, other_transcript) == (False, {False})
