"""Chat log import, alignment mapping, storage, deletion and the range endpoint."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from app.chat_import import Alignment, ChatImportError, Segment, manual_alignment, read_chat_log, replace_chat
from app.settings import settings
from app.source_deletion import delete_source
from scripts import import_chat

FIXTURE = Path(__file__).parent / "fixtures" / "chat" / "twitchdownloader.json"
START = datetime(2026, 11, 7, 18, 2, 11, tzinfo=timezone.utc)


def _video(db, duration_seconds=None):
    job_id, video_id = uuid.uuid4(), uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO jobs (id, kind, input_url, state) VALUES (:id, 'single', 'https://example.test', 'completed')"
        ),
        {"id": str(job_id)},
    )
    db.execute(
        text("""
            INSERT INTO videos (id, job_id, youtube_id, idx, title, state, duration_seconds)
            VALUES (:id, :job, :yt, 0, 't', 'completed', :duration)
        """),
        {"id": str(video_id), "job": str(job_id), "yt": video_id.hex[:11], "duration": duration_seconds},
    )
    return str(video_id)


def _stored(db, video_id):
    return db.execute(
        text("SELECT offset_ms, author, body FROM chat_messages WHERE video_id=:v ORDER BY offset_ms, id"),
        {"v": video_id},
    ).all()


def _write(tmp_path, document, name="chat.json"):
    path = tmp_path / name
    path.write_text(json.dumps(document))
    return path


def _rustlog(*messages):
    return {"messages": list(messages)}


def _rust(seconds, text_, name="RustViewer", kind=1, **extra):
    stamp = START.timestamp() + seconds
    timestamp = datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"type": kind, "timestamp": timestamp, "displayName": name, "username": name.lower(), "text": text_, **extra}


# Parsing and validation


def test_twitchdownloader_fixture_parses_authors_times_and_metadata():
    log = read_chat_log(FIXTURE, "twitchdownloader")

    assert [(m.source_ms, m.author, m.body) for m in log.messages] == [
        (10_000, "ViewerOne", "hello chat Kappa"),
        (110_500, "lowercase_login", "second message"),
        (300_250, "ViewerThree", "late message"),
    ]
    assert log.messages[0].metadata == {
        "color": "#1E90FF",
        "badges": [{"id": "subscriber", "version": "12"}],
        "emotes": [{"id": "25", "text": "Kappa"}],
    }
    assert log.messages[2].metadata is None  # an invalid colour is dropped, not stored
    assert len(log.digest) == 64 and log.source_name == "twitchdownloader.json"


def test_rustlog_export_uses_broadcast_start_and_keeps_only_chat(tmp_path):
    path = _write(
        tmp_path,
        _rustlog(
            _rust(-5, "before the stream"),
            _rust(2.5, "first", tags={"color": "#FF0000", "badges": "moderator/1,premium/1"}),
            _rust(3, "timeout notice", kind=2),
            {**_rust(4, "nanoseconds"), "timestamp": "2026-11-07T18:02:15.123456789Z"},
        ),
    )
    log = read_chat_log(path, "rustlog", broadcast_start=START)

    assert [(m.source_ms, m.body) for m in log.messages] == [(2500, "first"), (4123, "nanoseconds")]
    assert log.messages[0].metadata == {
        "color": "#FF0000",
        "badges": [{"id": "moderator", "version": "1"}, {"id": "premium", "version": "1"}],
    }
    assert log.skipped == {"not_chat": 1, "before_broadcast": 1}
    assert log.broadcast_started_at == START


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("{not json", "not valid JSON"),
        ({"messages": []}, "no 'comments' list"),
        ({"comments": [{"content_offset_seconds": -1}]}, "content_offset_seconds"),
        ({"comments": [{"content_offset_seconds": "10"}]}, "content_offset_seconds"),
        (
            {"comments": [{"content_offset_seconds": 1, "commenter": {"display_name": "a"}, "message": {"body": ""}}]},
            "text must be",
        ),
        (
            {"comments": [{"content_offset_seconds": 1, "commenter": {}, "message": {"body": "hi"}}]},
            "author must be",
        ),
        (
            {"comments": [{"content_offset_seconds": 1, "commenter": {"name": "a"}, "message": {"body": "x" * 1001}}]},
            "text must be",
        ),
        ({"comments": []}, "no chat messages"),
    ],
)
def test_malformed_twitchdownloader_input_is_rejected_with_the_reason(tmp_path, document, message):
    path = tmp_path / "chat.json"
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    with pytest.raises(ChatImportError, match=message):
        read_chat_log(path, "twitchdownloader")


def test_oversize_file_and_format_arguments_are_rejected(tmp_path):
    with pytest.raises(ChatImportError, match="the limit is 100"):
        read_chat_log(FIXTURE, "twitchdownloader", max_bytes=100)
    with pytest.raises(ChatImportError, match="need --broadcast-start"):
        read_chat_log(FIXTURE, "rustlog")
    with pytest.raises(ChatImportError, match="must not set it"):
        read_chat_log(FIXTURE, "twitchdownloader", broadcast_start=START)
    path = _write(tmp_path, _rustlog({**_rust(1, "x"), "timestamp": "yesterday"}))
    with pytest.raises(ChatImportError, match="ISO 8601"):
        read_chat_log(path, "rustlog", broadcast_start=START)


def test_alignment_maps_offsets_and_piecewise_cuts():
    assert manual_alignment(5000).map(0) == 5000
    later = manual_alignment(-30_000)  # recording starts 30 s into the broadcast
    assert later.map(29_999) is None and later.map(30_000) == 0 and later.map(90_000) == 60_000

    cut = Alignment(
        "audio",
        (Segment(60_000, 320_000, 0), Segment(440_000, 720_000, 260_000)),
        confidence=0.9,
    )
    assert cut.map(59_999) is None
    assert cut.map(100_000) == 40_000
    assert cut.map(330_000) is None  # removed by the edit
    assert cut.map(500_000) == 320_000
    assert cut.map(720_000) is None


# Storage, re-import and deletion


def test_import_stores_messages_at_recording_times(db_session):
    db = db_session
    video_id = _video(db, duration_seconds=250)
    result = replace_chat(db, video_id, read_chat_log(FIXTURE, "twitchdownloader"), manual_alignment(-5000))

    # The 300.25 s message lands at 295.25 s, after the 250 s recording ends.
    assert _stored(db, video_id) == [
        (5000, "ViewerOne", "hello chat Kappa"),
        (105_500, "lowercase_login", "second message"),
    ]
    assert result["messages_stored"] == 2
    assert result["dropped"] == {"outside_alignment": 0, "after_recording_end": 1}
    record = db.execute(text("SELECT * FROM chat_imports WHERE video_id=:v"), {"v": video_id}).mappings().one()
    assert record["source_format"] == "twitchdownloader"
    assert record["source_digest"] == result["source_digest"]
    assert record["alignment_method"] == "manual" and record["alignment_confidence"] is None
    assert record["alignment_mapping"] == [{"source_start_ms": 5000, "source_end_ms": None, "recording_start_ms": 0}]
    assert record["message_count"] == 2


def test_reimport_replaces_rather_than_duplicates(db_session, tmp_path):
    db = db_session
    video_id = _video(db)
    replace_chat(db, video_id, read_chat_log(FIXTURE, "twitchdownloader"), manual_alignment(0))
    log = read_chat_log(_write(tmp_path, _rustlog(_rust(1, "replacement"))), "rustlog", broadcast_start=START)
    alignment = Alignment("audio", (Segment(0, 10_000, 2000),), confidence=0.83)
    replace_chat(db, video_id, log, alignment)

    assert _stored(db, video_id) == [(3000, "RustViewer", "replacement")]
    record = db.execute(
        text(
            "SELECT source_format, alignment_method, alignment_confidence, broadcast_started_at FROM chat_imports"
            " WHERE video_id=:v"
        ),
        {"v": video_id},
    ).one()
    assert record == ("rustlog", "audio", 0.83, START)


def test_failed_import_leaves_the_previous_import(db_session):
    db = db_session
    video_id = _video(db)
    replace_chat(db, video_id, read_chat_log(FIXTURE, "twitchdownloader"), manual_alignment(0))
    with pytest.raises(ChatImportError, match="no messages fall"):
        replace_chat(db, video_id, read_chat_log(FIXTURE, "twitchdownloader"), manual_alignment(-10_000_000))
    with pytest.raises(ChatImportError, match="not found"):
        replace_chat(db, str(uuid.uuid4()), read_chat_log(FIXTURE, "twitchdownloader"), manual_alignment(0))
    assert len(_stored(db, video_id)) == 3


def test_deleting_the_source_recording_removes_its_chat(db_session):
    db = db_session
    video_id = _video(db)
    replace_chat(db, video_id, read_chat_log(FIXTURE, "twitchdownloader"), manual_alignment(0))
    assert delete_source(db, video_id=video_id, deleted_by_user_id=None)
    assert db.execute(text("SELECT count(*) FROM chat_imports WHERE video_id=:v"), {"v": video_id}).scalar() == 0
    assert _stored(db, video_id) == []


# Operator CLI


def test_cli_imports_with_a_manual_offset(db_session, monkeypatch, capsys):
    video_id = _video(db_session)
    monkeypatch.setattr(import_chat, "SessionLocal", lambda: db_session)
    argv = ["import", "--video-id", video_id, "--chat", str(FIXTURE), "--format", "twitchdownloader"]

    assert import_chat.main([*argv, "--offset-ms", "1000"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["messages_stored"] == 3 and output["dry_run"] is False
    assert _stored(db_session, video_id)[0][0] == 11_000


def test_cli_reports_invalid_input_without_a_traceback(tmp_path, capsys):
    bad = _write(tmp_path, {"comments": "nope"})
    code = import_chat.main(
        ["import", "--video-id", str(uuid.uuid4()), "--chat", str(bad), "--format", "twitchdownloader"]
        + ["--offset-ms", "0"]
    )
    assert code == 2
    assert "no 'comments' list" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        import_chat.main(["align", "--broadcast-audio", str(bad)])  # --recording-audio is required with it


# Endpoint


@pytest.fixture
def chat_enabled(monkeypatch):
    monkeypatch.setattr(settings, "CHAT_REPLAY_ENABLED", True)


def test_chat_endpoint_is_absent_when_the_flag_is_off(client, db_session):
    video_id = _video(db_session)
    response = client.get(f"/videos/{video_id}/chat", params={"start_ms": 0, "end_ms": 1000})
    assert response.status_code == 404
    assert response.json()["detail"] == "Chat replay is not enabled"


def test_chat_endpoint_reports_no_import_and_missing_video(client, db_session, chat_enabled):
    video_id = _video(db_session)
    response = client.get(f"/videos/{video_id}/chat", params={"start_ms": 0, "end_ms": 1000})
    assert response.status_code == 200
    assert response.json()["imported"] is False and response.json()["messages"] == []
    missing = client.get(f"/videos/{uuid.uuid4()}/chat", params={"start_ms": 0, "end_ms": 1000})
    assert missing.status_code == 404
    bad_range = client.get(f"/videos/{video_id}/chat", params={"start_ms": 1000, "end_ms": 1000})
    assert bad_range.status_code == 422
    too_many = client.get(f"/videos/{video_id}/chat", params={"start_ms": 0, "end_ms": 1, "limit": 1001})
    assert too_many.status_code == 422


def test_chat_endpoint_returns_only_the_requested_range_in_order(client, db_session, chat_enabled):
    video_id = _video(db_session)
    replace_chat(db_session, video_id, read_chat_log(FIXTURE, "twitchdownloader"), manual_alignment(0))
    response = client.get(f"/videos/{video_id}/chat", params={"start_ms": 10_000, "end_ms": 300_250})

    body = response.json()
    assert response.status_code == 200
    assert body["imported"] is True and body["truncated"] is False and body["next_start_ms"] is None
    assert body["chat_import"]["alignment_method"] == "manual" and body["chat_import"]["message_count"] == 3
    assert body["messages"] == [
        {
            "offset_ms": 10_000,
            "author": "ViewerOne",
            "text": "hello chat Kappa",
            "color": "#1E90FF",
            "badges": [{"id": "subscriber", "version": "12"}],
            "emotes": [{"id": "25", "text": "Kappa"}],
        },
        {
            "offset_ms": 110_500,
            "author": "lowercase_login",
            "text": "second message",
            "color": None,
            "badges": [],
            "emotes": [],
        },
    ]


def test_chat_endpoint_bounds_the_count_and_resumes_without_gaps(client, db_session, chat_enabled, tmp_path):
    video_id = _video(db_session)
    times = [1, 2, 2, 2, 3, 4]
    log = read_chat_log(
        _write(tmp_path, _rustlog(*[_rust(t, f"m{i}") for i, t in enumerate(times)])), "rustlog", broadcast_start=START
    )
    replace_chat(db_session, video_id, log, manual_alignment(0))

    seen, start = [], 0
    for _ in range(10):
        body = client.get(f"/videos/{video_id}/chat", params={"start_ms": start, "end_ms": 60_000, "limit": 3}).json()
        assert len(body["messages"]) <= 3
        seen += [message["text"] for message in body["messages"]]
        if not body["truncated"]:
            break
        start = body["next_start_ms"]
    assert seen == [f"m{i}" for i in range(len(times))]
