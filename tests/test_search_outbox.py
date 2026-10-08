import uuid

from sqlalchemy import text

from app.search.outbox import search_freshness, search_index_freshness


def test_segment_writes_emit_versioned_upsert_and_delete_tombstone(db_session):
    job_id, video_id = uuid.uuid4(), uuid.uuid4()
    db_session.execute(
        text("INSERT INTO jobs(id,kind,input_url) VALUES (:id,'single','https://youtube.com/x')"), {"id": job_id}
    )
    db_session.execute(
        text("INSERT INTO videos(id,job_id,youtube_id) VALUES (:id,:job,'outbox-video')"),
        {"id": video_id, "job": job_id},
    )
    segment_id = db_session.execute(
        text("INSERT INTO segments(video_id,start_ms,end_ms,text) VALUES (:video,0,1000,'hello') RETURNING id"),
        {"video": video_id},
    ).scalar_one()
    upsert = (
        db_session.execute(
            text("SELECT operation, version, payload->>'text' AS text FROM search_index_outbox WHERE document_id=:id"),
            {"id": segment_id},
        )
        .mappings()
        .one()
    )
    assert upsert["operation"] == "upsert"
    assert upsert["version"] > 0
    assert upsert["text"] == "hello"

    db_session.execute(text("DELETE FROM segments WHERE id=:id"), {"id": segment_id})
    operations = (
        db_session.execute(
            text("SELECT operation FROM search_index_outbox WHERE document_id=:id ORDER BY id"), {"id": segment_id}
        )
        .scalars()
        .all()
    )
    assert operations == ["upsert", "delete"]
    assert search_freshness(db_session)["pending_documents"] == 2


def test_request_freshness_matches_diagnostics_without_loading_counts(db_session):
    job_id, video_id = uuid.uuid4(), uuid.uuid4()
    db_session.execute(
        text("INSERT INTO jobs(id,kind,input_url) VALUES (:id,'single','https://youtube.com/freshness')"),
        {"id": job_id},
    )
    db_session.execute(
        text("INSERT INTO videos(id,job_id,youtube_id) VALUES (:id,:job,'freshness-video')"),
        {"id": video_id, "job": job_id},
    )
    db_session.execute(
        text("INSERT INTO segments(video_id,start_ms,end_ms,text) VALUES (:video,0,1000,'pending')"),
        {"video": video_id},
    )
    db_session.execute(text("""
        UPDATE search_index_outbox SET created_at=now()-interval '5 minutes'
        WHERE video_id=:video
    """), {"video": video_id})
    metadata = search_index_freshness(db_session)
    diagnostic = search_freshness(db_session)
    assert metadata["indexed_at"] == diagnostic["indexed_at"]
    assert abs(metadata["index_lag_seconds"] - diagnostic["index_lag_seconds"]) <= 1
    assert diagnostic["pending_documents"] >= 1
    assert set(metadata) == {"indexed_at", "index_lag_seconds"}


def test_request_freshness_empty_backlog_and_checkpoint(db_session):
    db_session.execute(text("DELETE FROM search_index_outbox"))
    db_session.execute(text("DELETE FROM search_index_checkpoints"))
    assert search_index_freshness(db_session) == {"indexed_at": None, "index_lag_seconds": 0}
