"""Transcript engine provenance: API reporting and the provenance-based upgrade requeue."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from worker.maintenance import ModelUpgradePolicy, requeue_for_model_upgrade

ENGINE_COLUMNS = ("engine_model", "engine_backend", "engine_device", "engine_compute_type", "engine_version")


def _video(db, *, state="completed", canary=False):
    job_id, video_id = uuid.uuid4(), uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO jobs (id, kind, input_url, state) VALUES (:id, 'single', 'https://example.test', 'completed')"
        ),
        {"id": str(job_id)},
    )
    db.execute(
        text("""
            INSERT INTO videos (id, job_id, youtube_id, idx, title, state, diarization_error)
            VALUES (:id, :job, :yt, 0, 't', :state, :marker)
            """),
        {
            "id": str(video_id),
            "job": str(job_id),
            "yt": video_id.hex[:11],
            "state": state,
            "marker": "canary-test" if canary else None,
        },
    )
    return str(video_id)


def _transcript(db, video_id, *, model="large-v3", engine=None, created_at=None):
    engine = engine or {}
    db.execute(
        text("""
            INSERT INTO transcripts (
                video_id, model, language, detected_language, language_probability, created_at,
                engine_model, engine_backend, engine_device, engine_compute_type, engine_version
            )
            VALUES (:v, :m, 'en', 'en', 0.97, :created, :em, :eb, :ed, :ec, :ev)
            """),
        {
            "v": video_id,
            "m": model,
            "created": created_at or datetime.now(timezone.utc) - timedelta(hours=1),
            "em": engine.get("model"),
            "eb": engine.get("backend"),
            "ed": engine.get("device"),
            "ec": engine.get("compute_type"),
            "ev": engine.get("version"),
        },
    )


def _engine(model, backend="faster-whisper", version="1.1.1"):
    return {"model": model, "backend": backend, "device": "cuda", "compute_type": "float16", "version": version}


def _requeue(db, policy, *, limit=10, produced_before=None):
    rows = requeue_for_model_upgrade(
        db.connection(),
        policy=policy,
        produced_before=produced_before or datetime.now(timezone.utc),
        limit=limit,
    )
    return {str(row[0]): row[1] for row in rows}


def _state(db, video_id):
    return db.execute(text("SELECT state FROM videos WHERE id = :v"), {"v": video_id}).scalar_one()


def test_requeue_selects_by_recorded_engine_not_the_legacy_model_column(db_session):
    db = db_session
    # Recorded a fallback to medium while the legacy column says large-v3.
    fell_back = _video(db)
    _transcript(db, fell_back, model="large-v3", engine=_engine("medium"))
    current = _video(db)
    _transcript(db, current, engine=_engine("large-v3"))

    requeued = _requeue(db, ModelUpgradePolicy(target_model="large-v3"))

    assert requeued == {fell_back: "medium"}
    assert _state(db, fell_back) == "pending"
    assert _state(db, current) == "completed"


def test_requeue_handles_model_names_it_has_never_seen(db_session):
    db = db_session
    old = _video(db)
    _transcript(db, old, engine=_engine("large-v3"))
    new = _video(db)
    _transcript(db, new, engine=_engine("distil-large-v3.5"))

    requeued = _requeue(db, ModelUpgradePolicy(target_model="distil-large-v3.5"))

    assert requeued == {old: "large-v3"}


def test_legacy_rows_use_the_stored_model_and_count_as_unknown_engine(db_session):
    db = db_session
    legacy_small = _video(db)
    _transcript(db, legacy_small, model="small")
    legacy_target = _video(db)
    _transcript(db, legacy_target, model="large-v3")
    recorded = _video(db)
    _transcript(db, recorded, engine=_engine("large-v3", version="1.1.1"))

    assert _requeue(db, ModelUpgradePolicy(target_model="large-v3")) == {legacy_small: "small"}

    # Naming a backend or version also redoes rows whose engine was never recorded.
    by_version = _requeue(db, ModelUpgradePolicy(target_model="large-v3", target_version="1.1.1"))
    assert by_version == {legacy_target: "large-v3"}


def test_requeue_selects_by_backend_and_version(db_session):
    db = db_session
    torch_run = _video(db)
    _transcript(db, torch_run, engine=_engine("large-v3", backend="whisper", version="20250625"))
    old_version = _video(db)
    _transcript(db, old_version, engine=_engine("large-v3", version="1.0.0"))
    current = _video(db)
    _transcript(db, current, engine=_engine("large-v3", version="1.1.1"))

    requeued = _requeue(
        db, ModelUpgradePolicy(target_model="large-v3", target_backend="faster-whisper", target_version="1.1.1")
    )

    assert set(requeued) == {torch_run, old_version}


def test_requeue_is_bounded_oldest_first(db_session):
    db = db_session
    now = datetime.now(timezone.utc)
    videos = []
    for age in (3, 2, 1):
        video_id = _video(db)
        _transcript(db, video_id, engine=_engine("small"), created_at=now - timedelta(hours=age))
        videos.append(video_id)

    first = _requeue(db, ModelUpgradePolicy(target_model="large-v3"), limit=2)
    second = _requeue(db, ModelUpgradePolicy(target_model="large-v3"), limit=2)

    assert list(first) == videos[:2]
    assert list(second) == videos[2:]


def test_requeue_skips_recent_canary_and_unfinished_videos(db_session):
    db = db_session
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    recent = _video(db)
    _transcript(db, recent, engine=_engine("medium"), created_at=cutoff + timedelta(minutes=5))
    canary = _video(db, canary=True)
    _transcript(db, canary, engine=_engine("medium"))
    unfinished = _video(db, state="transcribing")
    _transcript(db, unfinished, engine=_engine("medium"))

    assert _requeue(db, ModelUpgradePolicy(target_model="large-v3"), produced_before=cutoff) == {}


def test_metadata_reports_the_recorded_engine(client, db_session):
    video_id = _video(db_session)
    _transcript(db_session, video_id, model="medium", engine=_engine("medium"))

    response = client.get(f"/videos/{video_id}/transcript/metadata")

    assert response.status_code == 200
    body = response.json()
    assert body["provenance"] == "recorded"
    assert body["model"] == "medium"
    assert body["detected_language"] == "en"
    assert body["engine"] == {
        "model": "medium",
        "backend": "faster-whisper",
        "device": "cuda",
        "compute_type": "float16",
        "version": "1.1.1",
    }


def test_metadata_reports_rows_from_before_provenance_as_unknown(client, db_session):
    video_id = _video(db_session)
    _transcript(db_session, video_id, model="large-v3")

    body = client.get(f"/videos/{video_id}/transcript/metadata").json()

    assert body["provenance"] == "unknown"
    assert body["engine"] is None
    assert body["model"] == "large-v3"


def test_metadata_for_missing_video_or_transcript(client, db_session):
    assert client.get(f"/videos/{uuid.uuid4()}/transcript/metadata").status_code == 404

    video_id = _video(db_session, state="transcribing")
    response = client.get(f"/videos/{video_id}/transcript/metadata")
    assert response.status_code == 409
    assert response.json()["error"] == "transcript_not_ready"


def test_existing_rows_keep_null_provenance_after_migration(db_session):
    columns = db_session.execute(text("""
            SELECT column_name, is_nullable FROM information_schema.columns
            WHERE table_name = 'transcripts' AND column_name LIKE 'engine_%'
            """)).all()

    assert sorted(columns) == sorted((name, "YES") for name in ENGINE_COLUMNS)
