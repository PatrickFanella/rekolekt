"""`include=words` on the transcript endpoint and the JSON export."""

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.main import app
from app.security import get_user_required

FASTER_WHISPER_WORDS = [
    {"word": " Hello", "start": 1.0, "end": 1.5, "probability": 0.97},
    {"word": " there.", "start": 1.5, "end": 3.0, "probability": 0.9},
]
OPENAI_WHISPER_WORDS = [{"word": " Second", "start": 3.0, "end": 3.6}, {"word": " line", "start": 3.6, "end": 5.0}]


def _video(db, *, owner=None, youtube_id=None):
    job_id, video_id = uuid.uuid4(), uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO jobs (id, kind, input_url, owner_user_id) VALUES (:id, 'single', 'https://example.test', :o)"
        ),
        {"id": str(job_id), "o": owner},
    )
    db.execute(
        text("INSERT INTO videos (id, job_id, youtube_id, idx, state) VALUES (:id, :job, :yt, 0, 'completed')"),
        {"id": str(video_id), "job": str(job_id), "yt": youtube_id or video_id.hex[:11]},
    )
    return video_id


def _segment(db, video_id, start_ms, end_ms, segment_text, words=None, raw_words=None):
    segment_id = db.execute(
        text(
            "INSERT INTO segments (video_id, start_ms, end_ms, text, speaker_label) "
            "VALUES (:v, :s, :e, :t, 'Speaker 1') RETURNING id"
        ),
        {"v": str(video_id), "s": start_ms, "e": end_ms, "t": segment_text},
    ).scalar_one()
    stored = json.dumps(words) if words is not None else raw_words
    if stored is not None:
        db.execute(
            text("INSERT INTO segment_words (segment_id, words) VALUES (:id, CAST(:w AS jsonb))"),
            {"id": segment_id, "w": stored},
        )


def _youtube_captions(db, video_id):
    transcript_id = uuid.uuid4()
    db.execute(
        text("INSERT INTO youtube_transcripts (id, video_id, language) VALUES (:id, :v, 'en')"),
        {"id": str(transcript_id), "v": str(video_id)},
    )
    db.execute(
        text(
            "INSERT INTO youtube_segments (youtube_transcript_id, start_ms, end_ms, text) "
            "VALUES (:t, 1000, 3000, 'Hello there.')"
        ),
        {"t": str(transcript_id)},
    )


@pytest.fixture
def native_video(db_session):
    video_id = _video(db_session)
    _segment(db_session, video_id, 1000, 3000, "Hello there.", words=FASTER_WHISPER_WORDS)
    _segment(db_session, video_id, 3000, 5000, "Second line", words=OPENAI_WHISPER_WORDS)
    db_session.commit()
    return video_id


@pytest.fixture
def session_cookie(db_session):
    user_id, token = uuid.uuid4(), secrets.token_urlsafe(32)
    db_session.execute(
        text(
            "INSERT INTO users (id, email, oauth_provider, oauth_subject, plan) "
            "VALUES (:id, :email, 'google', :sub, 'pro')"
        ),
        {"id": str(user_id), "email": f"words-{user_id}@example.com", "sub": f"words-{user_id}"},
    )
    db_session.execute(
        text("INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (:uid, :h, :exp)"),
        {
            "uid": str(user_id),
            "h": hashlib.sha256(token.encode()).hexdigest(),
            "exp": datetime.utcnow() + timedelta(days=1),
        },
    )
    db_session.commit()
    return {"tc_session": token}


def test_default_raw_response_keeps_its_exact_shape(client, native_video):
    response = client.get(f"/videos/{native_video}/transcript")

    assert response.status_code == 200
    assert response.headers["ETag"] == f'"{native_video}-raw-whisper"'
    assert response.json() == {
        "video_id": str(native_video),
        "segments": [
            {"start_ms": 1000, "end_ms": 3000, "text": "Hello there.", "speaker_label": "Speaker 1"},
            {"start_ms": 3000, "end_ms": 5000, "text": "Second line", "speaker_label": "Speaker 1"},
        ],
        "source": "whisper",
        "source_label": "Whisper transcript",
        # Transcript corrections (#62) always report corrected ranges; none here.
        "corrections": [],
    }


def test_default_formatted_response_has_no_word_fields(client, native_video):
    data = client.get(f"/videos/{native_video}/transcript?mode=formatted").json()

    assert "word_timings" not in data
    assert all(set(segment) == {"start_ms", "end_ms", "text", "speaker_label"} for segment in data["segments"])


def test_include_words_returns_both_backend_shapes_in_milliseconds(client, native_video):
    client.get(f"/videos/{native_video}/transcript")  # first response sets the analytics cookie
    default = client.get(f"/videos/{native_video}/transcript")
    response = client.get(f"/videos/{native_video}/transcript?include=words")

    assert response.status_code == 200
    assert response.headers["ETag"] == f'"{native_video}-raw-whisper-words"'
    assert response.headers["Cache-Control"] == default.headers["Cache-Control"]
    assert response.headers["Cache-Control"].startswith("public")
    data = response.json()
    assert (data["word_timings"], data["word_timings_note"]) == ("available", None)
    assert data["segments"][0] == {
        "start_ms": 1000,
        "end_ms": 3000,
        "text": "Hello there.",
        "speaker_label": "Speaker 1",
        "words": [
            {"start_ms": 1000, "end_ms": 1500, "text": "Hello", "probability": 0.97},
            {"start_ms": 1500, "end_ms": 3000, "text": "there.", "probability": 0.9},
        ],
    }
    assert data["segments"][1]["words"] == [
        {"start_ms": 3000, "end_ms": 3600, "text": "Second", "probability": None},
        {"start_ms": 3600, "end_ms": 5000, "text": "line", "probability": None},
    ]


def test_include_words_in_formatted_mode_keeps_blocks(client, native_video):
    response = client.get(f"/videos/{native_video}/transcript?mode=formatted&include=words")

    assert response.status_code == 200
    assert response.headers["ETag"] == f'"{native_video}-formatted-whisper-words"'
    data = response.json()
    assert data["blocks"] and data["text"]
    assert data["word_timings"] == "available"
    assert data["segments"][0]["words"][0]["text"] == "Hello"


def test_missing_and_malformed_rows_report_no_word_timings(client, db_session):
    video_id = _video(db_session)
    _segment(db_session, video_id, 0, 1000, "stored", words=[{"word": "stored", "start": 0.1, "end": 0.6}])
    _segment(db_session, video_id, 1000, 2000, "never stored")
    _segment(db_session, video_id, 2000, 3000, "malformed", raw_words='{"word": "not a list"}')
    _segment(db_session, video_id, 3000, 4000, "out of range", words=[{"word": "late", "start": 9.0, "end": 9.5}])
    db_session.commit()

    data = client.get(f"/videos/{video_id}/transcript?include=words").json()

    assert [segment["words"] is None for segment in data["segments"]] == [False, True, True, True]
    assert (data["word_timings"], data["word_timings_note"]) == ("partial", "not_stored")


def test_transcript_without_any_word_timings_is_unavailable(client, db_session):
    video_id = _video(db_session)
    _segment(db_session, video_id, 0, 1000, "fast preset")
    db_session.commit()

    data = client.get(f"/videos/{video_id}/transcript?include=words").json()

    assert data["segments"][0]["words"] is None
    assert (data["word_timings"], data["word_timings_note"]) == ("unavailable", "not_stored")


def test_youtube_captions_state_word_timings_are_unavailable(client, db_session):
    video_id = _video(db_session)
    _youtube_captions(db_session, video_id)
    db_session.commit()

    response = client.get(f"/videos/{video_id}/transcript?source=youtube&include=words")

    assert response.status_code == 200
    assert response.headers["ETag"] == f'"{video_id}-raw-youtube-words"'
    data = response.json()
    assert data["source"] == "youtube"
    assert data["segments"][0]["words"] is None
    assert (data["word_timings"], data["word_timings_note"]) == ("unavailable", "youtube_captions")


def test_merged_transcript_states_word_timings_are_unavailable(client, db_session, native_video):
    _youtube_captions(db_session, native_video)
    db_session.commit()

    data = client.get(f"/videos/{native_video}/transcript?source=merged&mode=formatted&include=words").json()

    assert data["source"] == "merged"
    assert all(segment["words"] is None for segment in data["segments"])
    assert (data["word_timings"], data["word_timings_note"]) == ("unavailable", "merged_transcript")


def test_include_words_rejects_cleaned_mode_and_unknown_values(client, native_video):
    cleaned = client.get(f"/videos/{native_video}/transcript?mode=cleaned&include=words")
    unknown = client.get(f"/videos/{native_video}/transcript?include=letters")

    assert cleaned.status_code == 422
    assert cleaned.json()["error"] == "validation_error"
    assert unknown.status_code == 422


def test_include_words_for_unknown_video_is_not_found(client):
    assert client.get(f"/videos/{uuid.uuid4()}/transcript?include=words").status_code == 404


def test_deleted_source_takes_its_word_timings_with_it(client, db_session, monkeypatch):
    owner = {"id": str(uuid.uuid4()), "email": "words-owner@example.com", "name": "Owner", "plan": "free"}
    db_session.execute(
        text(
            "INSERT INTO users (id, email, name, oauth_provider, oauth_subject, plan) "
            "VALUES (:id, :email, :name, 'google', :subject, :plan)"
        ),
        {**owner, "subject": f"words-{owner['id']}"},
    )
    video_id = _video(db_session, owner=owner["id"])
    _segment(db_session, video_id, 0, 1000, "bye", words=[{"word": "bye", "start": 0.1, "end": 0.5}])
    db_session.commit()
    monkeypatch.setattr("app.source_deletion.invalidate_video_data", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.source_deletion._delete_file", lambda _path: None)
    app.dependency_overrides[get_user_required] = lambda: owner
    try:
        assert client.delete(f"/videos/{video_id}").status_code == 204
    finally:
        app.dependency_overrides.pop(get_user_required, None)

    assert client.get(f"/videos/{video_id}/transcript?include=words").status_code == 404
    assert db_session.execute(text("SELECT count(*) FROM segment_words")).scalar_one() == 0


def test_json_export_default_shape_is_unchanged(client, native_video, session_cookie):
    data = client.get(f"/videos/{native_video}/transcript.json", cookies=session_cookie).json()

    assert set(data) == {"video_id", "source", "source_label", "segments"}
    assert set(data["segments"][0]) == {"start_ms", "end_ms", "text", "speaker_label"}


def test_json_export_includes_words_on_request(client, native_video, session_cookie):
    data = client.get(f"/videos/{native_video}/transcript.json?include=words", cookies=session_cookie).json()

    assert (data["word_timings"], data["word_timings_note"]) == ("available", None)
    assert data["segments"][1]["words"][1] == {"start_ms": 3600, "end_ms": 5000, "text": "line", "probability": None}


def test_json_export_of_captions_reports_word_timings_unavailable(client, db_session, session_cookie):
    video_id = _video(db_session)
    _youtube_captions(db_session, video_id)
    db_session.commit()

    data = client.get(f"/videos/{video_id}/transcript.json?include=words", cookies=session_cookie).json()

    assert data["source"] == "youtube"
    assert data["segments"][0]["words"] is None
    assert (data["word_timings"], data["word_timings_note"]) == ("unavailable", "youtube_captions")


def test_json_export_with_words_still_requires_authentication(client, native_video):
    assert client.get(f"/videos/{native_video}/transcript.json?include=words").status_code == 401
