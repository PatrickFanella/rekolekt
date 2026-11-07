import hashlib
import json
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.clip_exports import digest
from app.clip_handoff import TranscriptUnavailableError, snapshot_transcript
from app.main import app
from app.routes.clips import ClipHandoff
from app.security import require_auth
from app.settings import settings
from tests.test_clip_exports import clip_store, fake_ytdlp, register, youtube_store  # noqa: F401

FIXTURE = Path(__file__).resolve().parents[1] / "docs" / "api" / "examples" / "clip-handoff.v1.json"
FORBIDDEN = ("owner_id", "rights_note", "relative_path", "/download", "token", "session", "cookie")


def cue_digest(cues):
    lines = (
        json.dumps([c["start_ms"], c["end_ms"], c["text"]], ensure_ascii=False, separators=(",", ":")) for c in cues
    )
    return hashlib.sha256("".join(line + "\n" for line in lines).encode()).hexdigest()


def add_video(db, video_id, *, youtube_id="BaW_jenozKc", duration=3):
    job = str(uuid.uuid4())
    db.execute(text("INSERT INTO jobs(id,kind,input_url) VALUES (:id,'single','https://example.invalid')"), {"id": job})
    db.execute(
        text("INSERT INTO videos(id,job_id,youtube_id,duration_seconds) VALUES (:id,:job,:yt,:d)"),
        {"id": video_id, "job": job, "yt": youtube_id, "d": duration},
    )


def add_whisper(db, video_id, rows):
    transcript = str(uuid.uuid4())
    db.execute(
        text("INSERT INTO transcripts(id,video_id,language,model) VALUES (:id,:v,'en','large-v3')"),
        {"id": transcript, "v": video_id},
    )
    for start, end, value in rows:
        db.execute(
            text("INSERT INTO segments(video_id,transcript_id,start_ms,end_ms,text) VALUES (:v,:t,:s,:e,:x)"),
            {"v": video_id, "t": transcript, "s": start, "e": end, "x": value},
        )
    return transcript


def add_captions(db, video_id, rows):
    transcript = str(uuid.uuid4())
    db.execute(
        text("INSERT INTO youtube_transcripts(id,video_id,language,kind) VALUES (:id,:v,'en','auto')"),
        {"id": transcript, "v": video_id},
    )
    for start, end, value in rows:
        db.execute(
            text("INSERT INTO youtube_segments(youtube_transcript_id,start_ms,end_ms,text) VALUES (:t,:s,:e,:x)"),
            {"t": transcript, "s": start, "e": end, "x": value},
        )
    return transcript


def test_snapshot_clips_cues_to_the_selection_and_digests_the_revision(db_session):
    video = str(uuid.uuid4())
    add_video(db_session, video, duration=20)
    whisper = add_whisper(
        db_session,
        video,
        [(0, 900, "before"), (800, 2200, "straddles start"), (2500, 2500, "untimed"), (2800, 4200, "straddles end")],
    )
    captions = add_captions(db_session, video, [(1000, 3000, "caption text")])

    snap = snapshot_transcript(db_session, video, 1000, 3000)
    transcript = snap["transcript"]
    assert transcript["source"] == "whisper" and transcript["transcript_id"] == whisper
    assert transcript["segment_count"] == 4 and transcript["timing"] == "segment"
    assert snap["cues"] == [
        {"start_ms": 0, "end_ms": 1200, "text": "straddles start"},
        {"start_ms": 1500, "end_ms": None, "text": "untimed"},
        {"start_ms": 1800, "end_ms": 2000, "text": "straddles end"},
    ]
    assert snap["cue_text_sha256"] == cue_digest(snap["cues"])
    assert snapshot_transcript(db_session, video, 1000, 3000)["transcript"]["revision_sha256"] == (
        transcript["revision_sha256"]
    )

    youtube = snapshot_transcript(db_session, video, 1000, 3000, "youtube")
    assert youtube["transcript"]["transcript_id"] == captions
    assert youtube["transcript"]["kind"] == "caption-auto"
    assert youtube["cues"] == [{"start_ms": 0, "end_ms": 2000, "text": "caption text"}]

    db_session.execute(text("UPDATE segments SET text='edited' WHERE video_id=:v AND start_ms=800"), {"v": video})
    assert snapshot_transcript(db_session, video, 1000, 3000)["transcript"]["revision_sha256"] != (
        transcript["revision_sha256"]
    )

    bare = str(uuid.uuid4())
    add_video(db_session, bare)
    unavailable = snapshot_transcript(db_session, bare, 0, 1000)
    assert unavailable["transcript"] == {"status": "unavailable"} and unavailable["cues"] == []
    with pytest.raises(TranscriptUnavailableError):
        snapshot_transcript(db_session, bare, 0, 1000, "youtube")


def test_snapshot_bounds_cue_count_and_text(db_session, monkeypatch):
    monkeypatch.setattr("app.clip_handoff.MAX_CUES", 2)
    monkeypatch.setattr("app.clip_handoff.MAX_CUE_CHARS", 5)
    video = str(uuid.uuid4())
    add_video(db_session, video)
    add_whisper(db_session, video, [(0, 100, "first long"), (100, 200, "two"), (200, 300, "three")])
    snap = snapshot_transcript(db_session, video, 0, 1000)
    assert snap["cues_truncated"] is True
    assert snap["cues"][0] == {"start_ms": 0, "end_ms": 100, "text": "first", "text_truncated": True}
    assert len(snap["cues"]) == 2


def enable_youtube(monkeypatch, store):
    monkeypatch.setattr(settings, "CLIP_EXPORTS_ENABLED", True)
    monkeypatch.setattr(settings, "CLIP_EXPORT_SOURCE", "youtube")
    monkeypatch.setattr(settings, "CLIP_SPOOL_DIR", str(store.spool))
    monkeypatch.setattr(settings, "CLIP_ORIGINALS_ROOT", "")
    monkeypatch.setattr(settings, "FRONTEND_ORIGIN", "https://archive.example")


def test_handoff_keeps_request_time_provenance_and_fails_like_download(
    client, db_session, youtube_store, monkeypatch  # noqa: F811
):
    store, media, video, owner = youtube_store
    enable_youtube(monkeypatch, store)
    fake_ytdlp(monkeypatch, media)
    db_session.execute(text("INSERT INTO users(id,name) VALUES (:id,'Clip owner')"), {"id": owner})
    add_video(db_session, video)
    add_whisper(db_session, video, [(0, 1500, "selected words"), (1500, 3000, "more words")])
    request = {"video_id": video, "start_ms": 500, "end_ms": 2500}
    app.dependency_overrides[require_auth] = lambda: {"id": owner, "role": "user"}
    try:
        job_id = client.post("/clips", json=request).json()["id"]
        assert client.get(f"/clips/{job_id}/handoff").status_code == 404  # incomplete
        assert store.run(job_id)["status"] == "completed"

        response = client.get(f"/clips/{job_id}/handoff")
        assert response.status_code == 200, response.text
        assert "no-store" in response.headers["cache-control"]
        handoff = response.json()
        ClipHandoff.model_validate(handoff)
        body = response.text
        assert not any(term in body for term in FORBIDDEN) and owner not in body
        assert handoff["schema"] == "rekolekt.clip-handoff" and handoff["schema_version"] == 1
        assert handoff["recording"] == {"video_id": video, "youtube_id": "BaW_jenozKc"}
        assert handoff["passage"]["url"] == (
            f"https://archive.example/api/share/videos/{video}?start_ms=500&end_ms=2500"
        )
        assert handoff["passage"]["duration_ms"] == 2000
        assert handoff["captions"]["cues"] == [
            {"start_ms": 0, "end_ms": 1000, "text": "selected words"},
            {"start_ms": 1000, "end_ms": 2000, "text": "more words"},
        ]
        assert handoff["captions"]["text_sha256"] == cue_digest(handoff["captions"]["cues"])
        output = store.media_path(job_id)
        assert handoff["media"]["sha256"] == digest(output)
        assert handoff["media"]["bytes"] == output.stat().st_size
        assert handoff["provenance"]["media_source"] == {"kind": "youtube-range", "youtube_id": "BaW_jenozKc"}

        # A later transcript edit does not relabel the original selection.
        db_session.execute(text("UPDATE segments SET text='rewritten' WHERE video_id=:v"), {"v": video})
        again = client.get(f"/clips/{job_id}/handoff").json()
        assert again["transcript"] == handoff["transcript"] and again["captions"] == handoff["captions"]
        # A new request against the changed transcript is a new export with its own snapshot.
        newer = client.post("/clips", json=request).json()["id"]
        assert newer != job_id
        assert client.post("/clips", json={**request, "transcript_source": "youtube"}).status_code == 422

        app.dependency_overrides[require_auth] = lambda: {"id": str(uuid.uuid4()), "role": "admin"}
        assert client.get(f"/clips/{job_id}/handoff").status_code == 404
        app.dependency_overrides[require_auth] = lambda: {"id": owner, "role": "user"}

        record = store.load("jobs", job_id)
        legacy = {key: value for key, value in record.items() if key != "provenance"}
        store.save("jobs", job_id, legacy)
        assert client.get(f"/clips/{job_id}/handoff").status_code == 409
        store.save("jobs", job_id, record)

        output.write_bytes(output.read_bytes() + b"x")
        assert client.get(f"/clips/{job_id}/handoff").status_code == 404
        assert client.get(f"/clips/{job_id}/download").status_code == 404
        output.write_bytes(output.read_bytes()[:-1])
        assert client.get(f"/clips/{job_id}/handoff").status_code == 200

        store.save("jobs", job_id, {**record, "expires_at": time.time() - 1})
        assert client.get(f"/clips/{job_id}/handoff").status_code == 404
        store.save("jobs", job_id, record)

        monkeypatch.setattr(settings, "CLIP_EXPORTS_ENABLED", False)
        assert client.get(f"/clips/{job_id}/handoff").status_code == 404
        monkeypatch.setattr(settings, "CLIP_EXPORTS_ENABLED", True)

        db_session.execute(text("DELETE FROM videos WHERE id=:id"), {"id": video})
        assert client.get(f"/clips/{job_id}/handoff").status_code == 404
    finally:
        app.dependency_overrides.pop(require_auth, None)


def test_original_handoff_omits_rights_note_and_stops_after_revocation(
    client, db_session, clip_store, monkeypatch  # noqa: F811
):
    store, media, video, owner = register(clip_store)
    monkeypatch.setattr(settings, "CLIP_EXPORTS_ENABLED", True)
    monkeypatch.setattr(settings, "CLIP_EXPORT_SOURCE", "original")
    monkeypatch.setattr(settings, "CLIP_ORIGINALS_ROOT", str(store.originals))
    monkeypatch.setattr(settings, "CLIP_SPOOL_DIR", str(store.spool))
    db_session.execute(text("INSERT INTO users(id,name) VALUES (:id,'Original owner')"), {"id": owner})
    add_video(db_session, video)
    app.dependency_overrides[require_auth] = lambda: {"id": owner, "role": "user"}
    try:
        job_id = client.post("/clips", json={"video_id": video, "start_ms": 0, "end_ms": 1000}).json()["id"]
        store.run(job_id)
        response = client.get(f"/clips/{job_id}/handoff")
        assert response.status_code == 200, response.text
        handoff = response.json()
        assert handoff["transcript"]["status"] == "unavailable"
        assert handoff["captions"] == {
            "timing": "unavailable",
            "cues": [],
            "cues_truncated": False,
            "text_sha256": None,
        }
        source = handoff["provenance"]["media_source"]
        assert source == {
            "kind": "registered-original",
            "original_sha256": store.load("sources", video)["sha256"],
            "timeline_aligned": True,
        }
        assert "Synthetic test pattern" not in response.text and "generated.mp4" not in response.text
        store.revoke(video)
        assert client.get(f"/clips/{job_id}/handoff").status_code == 404
    finally:
        app.dependency_overrides.pop(require_auth, None)


def test_committed_fixture_matches_the_contract():
    handoff = json.loads(FIXTURE.read_text())
    ClipHandoff.model_validate(handoff)
    assert handoff["captions"]["text_sha256"] == cue_digest(handoff["captions"]["cues"])
    assert not any(term in FIXTURE.read_text() for term in FORBIDDEN)
