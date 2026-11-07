import os
import subprocess  # nosec B404 - fixed interpreter and arguments
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app import crud
from app.accounts import delete_account
from app.clip_handoff import snapshot_transcript
from app.main import app
from app.routes.corrections import router as corrections_router
from app.security import require_auth
from app.source_deletion import delete_source
from app.transcript_corrections import reapply_corrections
from tests.test_clip_handoff import add_captions, add_video, add_whisper

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def corrections(client, db_session):
    routes = list(app.router.routes)
    app.include_router(corrections_router)
    users = [{"id": uuid.uuid4(), "role": role, "name": role} for role in ("user", "moderator", "user")]
    for user in users:
        db_session.execute(text("INSERT INTO users(id,role,name) VALUES (:id,:role,:name)"), user)
    video = str(uuid.uuid4())
    add_video(db_session, video, duration=20)
    add_whisper(db_session, video, [(0, 1000, "the cat sat"), (1000, 2000, "on the mat"), (2000, 3000, "for hours")])
    add_captions(db_session, video, [(0, 1500, "the cap sat"), (1500, 3000, "on them at")])
    current = {"user": users[0]}
    app.dependency_overrides[require_auth] = lambda: current["user"]
    yield client, db_session, users, current, video
    app.dependency_overrides.pop(require_auth, None)
    app.router.routes[:] = routes
    app.openapi_schema = None


def propose(client, video, text_value, start=0, end=1000, source="whisper"):
    return client.post(
        "/corrections",
        json={"video_id": video, "source": source, "start_ms": start, "end_ms": end, "text": text_value},
    )


def segment(db, video, start):
    return db.execute(
        text("SELECT text FROM segments WHERE video_id=:v AND start_ms=:s"), {"v": video, "s": start}
    ).scalar()


@pytest.mark.parametrize("enabled", ["false", "true"])
def test_flag_controls_route_registration(enabled):
    program = (
        "from unittest.mock import patch\n"
        "with patch('app.ytdlp_validation.validate_js_runtime_or_exit'):\n"
        "    from app.main import app\n"
        "print(sorted(p for p in app.openapi()['paths'] if p.endswith('corrections') or '/corrections/' in p))\n"
    )
    env = os.environ | {"TRANSCRIPT_CORRECTIONS_ENABLED": enabled, "ENVIRONMENT": "test"}
    result = subprocess.run(  # nosec B603 - fixed interpreter and arguments
        [sys.executable, "-c", program], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    )
    if enabled == "false":
        assert result.stdout.strip() == "[]"
    else:
        assert "/corrections" in result.stdout and "/videos/{video_id}/corrections" in result.stdout


def test_default_app_registers_no_correction_routes(client):
    assert client.post("/corrections", json={}).status_code == 404
    assert client.get(f"/videos/{uuid.uuid4()}/corrections").status_code == 404


def test_guests_cannot_propose_and_bounds_apply(corrections):
    client, db, users, current, video = corrections
    app.dependency_overrides.pop(require_auth)
    assert propose(client, video, "the hat sat").status_code == 401
    app.dependency_overrides[require_auth] = lambda: current["user"]
    assert propose(client, video, "x" * 2001).status_code == 422
    assert propose(client, video, "   ").status_code == 422
    assert propose(client, video, "the cat sat").status_code == 422
    assert propose(client, video, "the hat sat", start=0, end=999).status_code == 404
    assert propose(client, str(uuid.uuid4()), "the hat sat").status_code == 404
    assert propose(client, video, "the hat sat", start=10, end=5).status_code == 422
    for _ in range(10):
        assert propose(client, video, "the hat sat").status_code == 201
    assert propose(client, video, "the hat sat").status_code == 429


def test_authorization_matrix_and_audited_reject(corrections):
    client, db, users, current, video = corrections
    proposal = propose(client, video, "the hat sat").json()
    assert proposal["status"] == "pending" and proposal["machine_text"] == "the cat sat"
    for call in (
        lambda: client.get("/corrections/queue"),
        lambda: client.post(f"/corrections/{proposal['id']}/accept", json={"reason": "ok"}),
        lambda: client.post(f"/corrections/{proposal['id']}/reject", json={"reason": "no"}),
    ):
        assert call().status_code == 403
    current["user"] = users[2]
    propose(client, video, "the hat sat")
    current["user"] = users[1]
    queue = client.get("/corrections/queue").json()["items"]
    assert [item["supporters"] for item in queue] == [2, 2]
    assert "proposer_id" not in queue[0]
    assert client.post(f"/corrections/{proposal['id']}/reject", json={"reason": ""}).status_code == 422
    rejected = client.post(f"/corrections/{proposal['id']}/reject", json={"reason": "Audio says cat"})
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert client.post(f"/corrections/{proposal['id']}/accept", json={"reason": "ok"}).status_code == 409
    assert segment(db, video, 0) == "the cat sat"
    current["user"] = users[0]
    mine = client.get("/corrections/mine").json()["items"]
    assert [(m["status"], m["review_reason"]) for m in mine] == [("rejected", "Audio says cat")]
    audited = db.execute(
        text("SELECT details->>'reason' FROM audit_logs WHERE action='transcript_correction.reject'")
    ).scalar()
    assert audited == "Audio says cat"


def test_accepted_correction_reaches_reads_search_and_handoff(corrections):
    client, db, users, current, video = corrections
    before = snapshot_transcript(db, video, 0, 3000, "whisper")
    proposal = propose(client, video, "the hat sat").json()
    caption = propose(client, video, "the cat sat", start=0, end=1500, source="youtube").json()
    current["user"] = users[1]
    accepted = client.post(f"/corrections/{proposal['id']}/accept", json={"reason": "Matches audio"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"
    assert client.post(f"/corrections/{caption['id']}/accept", json={"reason": "ok"}).status_code == 200

    raw = client.get(f"/videos/{video}/transcript").json()
    assert raw["segments"][0]["text"] == "the hat sat"
    assert raw["corrections"] == [{"start_ms": 0, "end_ms": 1000, "machine_text": "the cat sat"}]
    formatted = client.get(f"/videos/{video}/transcript?mode=formatted").json()
    assert "hat" in formatted["blocks"][0]["text"] and formatted["corrections"]
    captions = client.get(f"/videos/{video}/youtube-transcript").json()
    assert captions["segments"][0]["text"] == "the cat sat" and captions["full_text"].startswith("the cat sat")
    assert captions["corrections"][0]["machine_text"] == "the cap sat"
    assert client.get(f"/videos/{video}/transcript?source=merged").json()["corrections"] == []

    assert (
        db.execute(text("SELECT full_text FROM transcripts WHERE video_id=:v"), {"v": video})
        .scalar()
        .startswith("the hat sat")
    )
    outbox = db.execute(
        text("SELECT payload->>'text' FROM search_index_outbox WHERE video_id=:v AND source='native' ORDER BY id DESC"),
        {"v": video},
    ).scalar()
    assert outbox == "the hat sat"
    assert [str(hit["video_id"]) for hit in crud.search_segments(db, "hat", video_id=video)] == [video]

    after = snapshot_transcript(db, video, 0, 3000, "whisper")
    assert after["transcript"]["revision_sha256"] != before["transcript"]["revision_sha256"]
    assert after["cues"][0]["text"] == "the hat sat"

    public = client.get(f"/videos/{video}/corrections").json()["items"]
    assert {(p["source"], p["machine_text"], p["corrected_text"]) for p in public} == {
        ("whisper", "the cat sat", "the hat sat"),
        ("youtube", "the cap sat", "the cat sat"),
    }
    assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='transcript_correction.accept'")).scalar() == 2


def test_later_acceptance_supersedes_and_keeps_machine_text(corrections):
    client, db, users, current, video = corrections
    first = propose(client, video, "the hat sat").json()
    current["user"] = users[1]
    client.post(f"/corrections/{first['id']}/accept", json={"reason": "ok"})
    current["user"] = users[2]
    second = propose(client, video, "the bat sat").json()
    assert second["machine_text"] == "the cat sat"
    current["user"] = users[1]
    assert client.post(f"/corrections/{second['id']}/accept", json={"reason": "better"}).status_code == 200
    assert segment(db, video, 0) == "the bat sat"
    statuses = dict(db.execute(text("SELECT corrected_text, status FROM segment_corrections")).all())
    assert statuses == {"the hat sat": "superseded", "the bat sat": "accepted"}


def test_accepting_against_changed_text_marks_proposal_stale(corrections):
    client, db, users, current, video = corrections
    proposal = propose(client, video, "the hat sat").json()
    db.execute(text("UPDATE segments SET text='the rat sat' WHERE video_id=:v AND start_ms=0"), {"v": video})
    current["user"] = users[1]
    response = client.post(f"/corrections/{proposal['id']}/accept", json={"reason": "ok"})
    assert response.status_code == 409
    assert segment(db, video, 0) == "the rat sat"
    stale = client.get("/corrections/queue?status=stale").json()["items"]
    assert [s["id"] for s in stale] == [proposal["id"]]
    assert client.post(f"/corrections/{proposal['id']}/reject", json={"reason": "obsolete"}).status_code == 200


def test_retranscription_reapplies_matching_and_flags_the_rest(corrections):
    client, db, users, current, video = corrections
    ids = [propose(client, video, "the hat sat").json()["id"]]
    ids.append(propose(client, video, "on a mat", start=1000, end=2000).json()["id"])
    current["user"] = users[1]
    for correction_id in ids:
        assert client.post(f"/corrections/{correction_id}/accept", json={"reason": "ok"}).status_code == 200

    # Re-transcription replaces every row: one range keeps its machine text, one changes.
    db.execute(text("DELETE FROM segments WHERE video_id=:v"), {"v": video})
    db.execute(text("DELETE FROM transcripts WHERE video_id=:v"), {"v": video})
    add_whisper(db, video, [(0, 1000, "the cat sat"), (1000, 2000, "on the mattress"), (2000, 3000, "for hours")])
    reapply_corrections(db, video, "whisper")

    assert segment(db, video, 0) == "the hat sat"
    assert segment(db, video, 1000) == "on the mattress"
    statuses = dict(db.execute(text("SELECT corrected_text, status FROM segment_corrections")).all())
    assert statuses == {"the hat sat": "accepted", "on a mat": "stale"}
    assert client.get("/corrections/queue?status=stale").json()["items"][0]["machine_text"] == "on the mat"
    marks = client.get(f"/videos/{video}/transcript").json()["corrections"]
    assert [m["start_ms"] for m in marks] == [0]

    # Applying twice changes nothing; a later run that restores the old text re-applies it.
    reapply_corrections(db, video, "whisper")
    assert segment(db, video, 0) == "the hat sat"
    db.execute(text("UPDATE segments SET text='on the mat' WHERE video_id=:v AND start_ms=1000"), {"v": video})
    reapply_corrections(db, video, "whisper")
    assert segment(db, video, 1000) == "on a mat"


def test_caption_reingest_reapplies_youtube_corrections(corrections):
    client, db, users, current, video = corrections
    proposal = propose(client, video, "the cat sat", start=0, end=1500, source="youtube").json()
    current["user"] = users[1]
    client.post(f"/corrections/{proposal['id']}/accept", json={"reason": "ok"})
    db.execute(text("DELETE FROM youtube_transcripts WHERE video_id=:v"), {"v": video})
    add_captions(db, video, [(0, 1500, "the cap sat"), (1500, 3000, "on them at")])
    reapply_corrections(db, video, "youtube")
    rows = crud.list_youtube_segments(db, crud.get_youtube_transcript(db, video)["id"])
    assert rows[0][2] == "the cat sat"


def test_account_deletion_keeps_accepted_text_and_removes_other_proposals(corrections):
    client, db, users, current, video = corrections
    accepted = propose(client, video, "the hat sat").json()
    pending = propose(client, video, "in the mat", start=1000, end=2000).json()
    current["user"] = users[1]
    client.post(f"/corrections/{accepted['id']}/accept", json={"reason": "ok"})
    delete_account(db, users[0]["id"])
    rows = db.execute(text("SELECT id, proposer_id FROM segment_corrections")).all()
    assert [(str(r[0]), r[1]) for r in rows] == [(accepted["id"], None)]
    assert pending["id"] not in {str(r[0]) for r in rows}
    assert segment(db, video, 0) == "the hat sat"
    delete_account(db, users[1]["id"])
    assert db.execute(text("SELECT reviewer_id FROM segment_corrections")).scalar() is None


def test_source_deletion_removes_corrections(corrections):
    client, db, users, current, video = corrections
    propose(client, video, "the hat sat")
    assert delete_source(db, video_id=video, deleted_by_user_id=users[1]["id"])
    assert db.execute(text("SELECT count(*) FROM segment_corrections")).scalar() == 0


def test_account_merge_carries_proposals(db_session):
    from app.accounts import merge_account_identity, sign_in_identity
    from tests.test_accounts import google_profile, twitch_profile

    target = sign_in_identity(db_session, google_profile("fix-target", "fix-target@example.com")).user["id"]
    source = sign_in_identity(db_session, twitch_profile("fix-source", "fix-source@example.com")).user["id"]
    video = str(uuid.uuid4())
    add_video(db_session, video)
    db_session.execute(
        text("""INSERT INTO segment_corrections(video_id,source,start_ms,end_ms,original_sha256,original_text,
        corrected_text,proposer_id,reviewer_id) VALUES (:v,'whisper',0,1,:d,'a','b',:s,:s)"""),
        {"v": video, "d": "0" * 64, "s": source},
    )
    merge_account_identity(
        db_session, target, twitch_profile("fix-source", "fix-source@example.com"), user_agent=None, ip_address=None
    )
    row = db_session.execute(text("SELECT proposer_id, reviewer_id FROM segment_corrections")).one()
    assert tuple(row) == (target, target)
