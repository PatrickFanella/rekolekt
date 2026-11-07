"""Podcasting 2.0 chapters document and recordings RSS feed."""

import hashlib
import json
import secrets
import uuid
import xml.etree.ElementTree as ET  # nosec B405 - parses the application's own output in tests
from datetime import date, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest
from sqlalchemy import text

from app.routes import podcast
from app.settings import settings
from app.source_deletion import delete_source

PODCAST = "{https://podcastindex.org/namespace/1.0}"
SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "podcast_json_chapters.schema.json").read_text())
ORIGIN = "https://archive.example"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "PODCAST_FEED_ENABLED", True)
    monkeypatch.setattr(settings, "FRONTEND_ORIGIN", ORIGIN)


def _video(db, *, title="Episode", state="completed", broadcast=date(2099, 1, 1), language="en"):
    job_id, video_id = uuid.uuid4(), uuid.uuid4()
    db.execute(
        text("INSERT INTO jobs (id, kind, input_url) VALUES (:id, 'single', 'https://youtube.com/test')"),
        {"id": str(job_id)},
    )
    db.execute(
        text("""
            INSERT INTO videos (id, job_id, youtube_id, idx, title, state, broadcast_date, language, channel_name)
            VALUES (:id, :job, :yt, 0, :title, :state, :broadcast, :language, 'Channel & Co')
        """),
        {
            "id": str(video_id),
            "job": str(job_id),
            "yt": video_id.hex[:11],
            "title": title,
            "state": state,
            "broadcast": broadcast,
            "language": language,
        },
    )
    return video_id


def _chapter(db, video_id, index, start_ms, end_ms, status="published", title="Chapter"):
    db.execute(
        text("""
            INSERT INTO archive_video_chapters (video_id, chapter_index, start_ms, end_ms, title, status, source)
            VALUES (:video, :index, :start, :end, :title, :status, 'automatic')
        """),
        {"video": str(video_id), "index": index, "start": start_ms, "end": end_ms, "title": title, "status": status},
    )


def _items(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    root = ET.fromstring(response.content)  # nosec B314 - trusted test output
    assert root.tag == "rss" and root.get("version") == "2.0"
    return {item.findtext("guid"): item for item in root.iter("item")}


def test_both_endpoints_are_disabled_by_default(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "PODCAST_FEED_ENABLED", False)
    video_id = _video(db_session)
    _chapter(db_session, video_id, 0, 0, 60_000)
    db_session.commit()

    assert client.get("/feeds/recordings.rss").status_code == 404
    assert client.get(f"/videos/{video_id}/podcast-chapters.json").status_code == 404


def test_chapters_document_matches_the_json_chapters_spec_and_lists_only_published(client, db_session, enabled):
    video_id = _video(db_session, title='Rally <night> & "more"')
    _chapter(db_session, video_id, 1, 90_500, 180_000, title="Second")
    _chapter(db_session, video_id, 0, 0, 90_500, title="Opening")
    _chapter(db_session, video_id, 2, 180_000, 240_000, status="candidate", title="Draft")
    _chapter(db_session, video_id, 3, 240_000, 300_000, status="hidden", title="Hidden")
    db_session.commit()

    response = client.get(f"/videos/{video_id}/podcast-chapters.json")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json+chapters"
    document = response.json()
    jsonschema.validate(document, SCHEMA)
    assert document["version"] == "1.2.0"
    assert document["title"] == 'Rally <night> & "more"'
    assert document["chapters"] == [
        {"startTime": 0.0, "endTime": 90.5, "title": "Opening"},
        {"startTime": 90.5, "endTime": 180.0, "title": "Second"},
    ]


def test_chapters_404_without_published_chapters_or_recording(client, db_session, enabled):
    video_id = _video(db_session)
    _chapter(db_session, video_id, 0, 0, 60_000, status="candidate")
    db_session.commit()

    assert client.get(f"/videos/{video_id}/podcast-chapters.json").status_code == 404
    assert client.get(f"/videos/{uuid.uuid4()}/podcast-chapters.json").status_code == 404


def test_feed_lists_completed_recordings_latest_first_with_transcript_and_chapter_links(client, db_session, enabled):
    older = _video(db_session, title="Older", broadcast=date(2099, 1, 1))
    newer = _video(db_session, title="Tom & Jerry <live> \x0b", broadcast=date(2099, 1, 2), language=None)
    pending = _video(db_session, title="Pending", state="transcribing", broadcast=date(2099, 1, 3))
    _chapter(db_session, newer, 0, 0, 60_000)
    _chapter(db_session, older, 0, 0, 60_000, status="candidate")
    db_session.commit()

    response = client.get("/feeds/recordings.rss")

    assert b'xmlns:podcast="https://podcastindex.org/namespace/1.0"' in response.content
    items = _items(response)
    assert str(pending) not in items
    guids = list(items)
    assert guids.index(str(newer)) < guids.index(str(older))

    item = items[str(newer)]
    assert item.findtext("title") == "Tom & Jerry <live> "
    assert item.findtext("link") == f"{ORIGIN}/v/{newer}"
    assert item.findtext("pubDate").startswith("Fri, 02 Jan 2099 00:00:00")
    assert item.findtext("description") == "Transcribed recording from Channel & Co."
    assert item.find("enclosure") is None
    transcripts = {node.get("type"): node.attrib for node in item.findall(f"{PODCAST}transcript")}
    assert transcripts == {
        "text/vtt": {"url": f"{ORIGIN}/api/videos/{newer}/transcript.vtt", "type": "text/vtt", "rel": "captions"},
        "application/x-subrip": {
            "url": f"{ORIGIN}/api/videos/{newer}/transcript.srt",
            "type": "application/x-subrip",
            "rel": "captions",
        },
    }
    chapters = item.find(f"{PODCAST}chapters")
    assert chapters.attrib == {
        "url": f"{ORIGIN}/api/videos/{newer}/podcast-chapters.json",
        "type": "application/json+chapters",
    }

    older_item = items[str(older)]
    assert older_item.find(f"{PODCAST}chapters") is None
    assert {node.get("language") for node in older_item.findall(f"{PODCAST}transcript")} == {"en"}


def test_feed_is_bounded(client, db_session, enabled, monkeypatch):
    monkeypatch.setattr(podcast, "FEED_LIMIT", 2)
    for day in (1, 2, 3):
        _video(db_session, broadcast=date(2099, 2, day))
    db_session.commit()

    assert len(_items(client.get("/feeds/recordings.rss"))) == 2


def test_deleted_source_leaves_the_feed_and_its_urls_return_404(client, db_session, enabled):
    user_id, session_token = uuid.uuid4(), secrets.token_urlsafe(32)
    db_session.execute(
        text(
            "INSERT INTO users (id, email, oauth_provider, oauth_subject, plan) "
            "VALUES (:id, :email, 'google', :subject, 'pro')"
        ),
        {"id": str(user_id), "email": f"{user_id.hex}@example.com", "subject": user_id.hex},
    )
    db_session.execute(
        text("INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (:uid, :token_hash, :exp)"),
        {
            "uid": str(user_id),
            "token_hash": hashlib.sha256(session_token.encode()).hexdigest(),
            "exp": datetime.utcnow() + timedelta(days=1),
        },
    )
    video_id = _video(db_session)
    _chapter(db_session, video_id, 0, 0, 60_000)
    db_session.execute(
        text("INSERT INTO segments (video_id, start_ms, end_ms, text) VALUES (:id, 0, 1000, 'hello')"),
        {"id": str(video_id)},
    )
    db_session.commit()
    assert str(video_id) in _items(client.get("/feeds/recordings.rss"))

    delete_source(db_session, video_id=video_id, deleted_by_user_id=user_id)
    db_session.commit()

    assert str(video_id) not in _items(client.get("/feeds/recordings.rss"))
    assert client.get(f"/videos/{video_id}/podcast-chapters.json").status_code == 404
    for suffix in ("vtt", "srt"):
        response = client.get(f"/videos/{video_id}/transcript.{suffix}", cookies={"tc_session": session_token})
        assert response.status_code == 404
