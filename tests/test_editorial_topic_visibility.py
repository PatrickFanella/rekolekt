"""Exercise editorial topic publication against real PostgreSQL queries."""

import json
import uuid

import pytest
from sqlalchemy import text

from app.archive.intelligence_repository import merge_label_topic_cards, published_label_cards_for_period


@pytest.fixture
def topic_db(db_session, monkeypatch):
    tables = {
        "archive_labels": "id uuid, slug text, label text, kind text, status text, source text",
        "archive_label_feedback": "label_id uuid, action text, user_id uuid, new_value jsonb",
        "archive_label_assignments": """id uuid, label_id uuid, video_id uuid, start_ms int, end_ms int,
            status text, publish_tier text, confidence_score numeric, evidence_count int, evidence jsonb""",
        "videos": """id uuid, youtube_id text, title text, duration_seconds int, state text,
            caption_ingest_state text, diarization_state text, uploaded_at timestamptz, broadcast_date date,
            broadcast_date_source text, broadcast_kind text, created_at timestamptz, updated_at timestamptz,
            channel_name text, language text, category text""",
        "segments": "video_id uuid",
        "youtube_transcripts": "video_id uuid",
    }
    for name, columns in tables.items():
        db_session.execute(text(f"CREATE TEMP TABLE {name} ({columns}) ON COMMIT DROP"))
    monkeypatch.setattr("app.archive.intelligence_repository._safe_video_metadata_map", lambda *_args: {})
    return db_session


def _seed_topic(db, source, feedback):
    label_id, video_id = uuid.uuid4(), uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO archive_labels VALUES (:id,'academic-freedom','Academic Freedom','topic','published',:source)"
        ),
        {"id": label_id, "source": source},
    )
    db.execute(
        text("""INSERT INTO videos(id,youtube_id,title,duration_seconds,state,broadcast_date,broadcast_kind)
            VALUES (:id,'fixture-video','Academic freedom discussion',600,'completed','2026-11-08','vod')"""),
        {"id": video_id},
    )
    db.execute(
        text("""INSERT INTO archive_label_assignments VALUES
            (:id,:label,:video,1000,4000,'auto_published','gold',0.98,1,
             '[{"snippet":"We are discussing academic freedom."}]'::jsonb)"""),
        {"id": uuid.uuid4(), "label": label_id, "video": video_id},
    )
    if feedback:
        db.execute(
            text("INSERT INTO archive_label_feedback VALUES (:label,:action,:actor,CAST(:value AS jsonb))"),
            {
                "label": label_id,
                "action": feedback["action"],
                "actor": uuid.uuid4() if feedback.get("actor") else None,
                "value": json.dumps({"status": "published", "review_origin": feedback.get("origin")}),
            },
        )

    return label_id


@pytest.mark.parametrize(
    ("source", "feedback", "visible"),
    [
        ("admin", None, True),
        ("seed", None, True),
        ("hybrid", None, True),
        ("automatic", {"action": "approve", "actor": True}, True),
        ("automatic", {"action": "publish", "actor": True}, True),
        ("automatic", {"action": "approve", "origin": "editorial"}, True),
        ("automatic", {"action": "publish", "origin": "editorial"}, True),
        ("automatic", None, False),
        ("automatic", {"action": "approve"}, False),
        ("automatic", {"action": "approve", "origin": "automatic"}, False),
        ("automatic", {"action": "merge_alias", "origin": "editorial"}, False),
        ("automatic", {"action": "reject", "actor": True}, False),
    ],
)
def test_public_topics_require_curated_source_or_editorial_approval(topic_db, source, feedback, visible):
    label_id = _seed_topic(topic_db, source, feedback)

    cards = published_label_cards_for_period(topic_db)
    public_cards = merge_label_topic_cards([], cards, 12)

    assert [c.slug for c in public_cards] == (["academic-freedom"] if visible else [])
    if visible:
        assert public_cards[0].evidence[0].snippet == "We are discussing academic freedom."
    assert topic_db.execute(text("SELECT source FROM archive_labels WHERE id=:id"), {"id": label_id}).scalar() == source


@pytest.mark.parametrize("label_status", ["candidate", "hidden", "rejected", "merged"])
def test_editorial_approval_does_not_override_current_label_status(topic_db, label_status):
    _seed_topic(topic_db, "automatic", {"action": "approve", "origin": "editorial"})
    topic_db.execute(text("UPDATE archive_labels SET status=:status"), {"status": label_status})
    assert published_label_cards_for_period(topic_db) == []


@pytest.mark.parametrize(
    ("assignment_status", "tier"),
    [("candidate", "gold"), ("rejected", "gold"), ("shadow", "shadow"), ("admin_approved", "bronze")],
)
def test_editorial_approval_does_not_publish_unqualified_assignments(topic_db, assignment_status, tier):
    _seed_topic(topic_db, "automatic", {"action": "approve", "origin": "editorial"})
    topic_db.execute(
        text("UPDATE archive_label_assignments SET status=:status,publish_tier=:tier"),
        {"status": assignment_status, "tier": tier},
    )
    assert published_label_cards_for_period(topic_db) == []
