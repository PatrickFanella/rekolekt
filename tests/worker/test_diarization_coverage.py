"""Diarization coverage: missing-audio re-fetch, stored centroids and bounded backlog runs."""

import uuid
from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import text

from worker import diarization_loop
from worker.diarize import RecordingDiarization


class _SessionEngine:
    """Run the worker's transactions as savepoints inside the test transaction."""

    def __init__(self, db_session):
        self.conn = db_session.connection()

    @contextmanager
    def begin(self):
        with self.conn.begin_nested():
            yield self.conn


def _insert_video(
    db_session,
    *,
    duration=60,
    diarization_state="pending",
    diarization_error=None,
    wav_path="/nonexistent/audio_16k.wav",
    created_offset_minutes=0,
):
    job_id, video_id = uuid.uuid4(), uuid.uuid4()
    db_session.execute(
        text("INSERT INTO jobs (id, kind, input_url) VALUES (:id, 'single', :url)"),
        {"id": job_id, "url": f"https://youtube.com/watch?v={job_id}"},
    )
    db_session.execute(
        text("""
            INSERT INTO videos (id, job_id, youtube_id, idx, state, wav_path, duration_seconds,
                                diarization_state, diarization_error, created_at)
            VALUES (:id, :job_id, :youtube_id, 0, 'completed', :wav_path, :duration, :state, :error,
                    now() - (:offset * interval '1 minute'))
        """),
        {
            "id": video_id,
            "job_id": job_id,
            "youtube_id": f"yt{str(video_id)[:9]}",
            "wav_path": wav_path,
            "duration": duration,
            "state": diarization_state,
            "error": diarization_error,
            "offset": created_offset_minutes,
        },
    )
    for start in (0, 2000):
        db_session.execute(
            text("INSERT INTO segments (video_id, start_ms, end_ms, text) VALUES (:v, :s, :e, 'words')"),
            {"v": video_id, "s": start, "e": start + 1000},
        )
    return video_id


def _result():
    return RecordingDiarization(
        tracks=[(0.0, 1.5, "Speaker 1"), (1.5, 4.0, "Speaker 2")],
        centroids={"Speaker 1": [1.0, 0.0], "Speaker 2": [0.0, 1.0]},
        speech_seconds={"Speaker 1": 1.5, "Speaker 2": 2.5},
    )


@contextmanager
def _worker_patches(result=None):
    with (
        patch.object(diarization_loop, "_start_diarization_heartbeat", return_value=(Mock(), Mock())),
        patch.object(diarization_loop, "_stop_diarization_heartbeat"),
        patch.object(diarization_loop, "invalidate_video_data"),
        patch.object(diarization_loop, "diarize_recording", return_value=result or _result()) as diarize,
    ):
        yield diarize


def _state(db_session, video_id):
    return db_session.execute(
        text("SELECT diarization_state, diarization_error FROM videos WHERE id = :v"), {"v": video_id}
    ).one()


def test_missing_audio_is_refetched_into_scratch_and_centroids_are_stored(db_session, monkeypatch, tmp_path):
    video_id = _insert_video(db_session)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_ALLOWED_VIDEO_IDS", frozenset({video_id}))
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_REFETCH_AUDIO", True)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_REFETCH_DIR", str(tmp_path))
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_MODEL", "test-model")
    seen = {}

    def download(url, dest):
        seen["url"], seen["dest"] = url, dest
        raw = dest / "raw.m4a"
        raw.write_bytes(b"raw")
        return raw

    def transcode(raw):
        wav = raw.parent / "audio_16k.wav"
        wav.write_bytes(b"wav")
        return wav

    with (
        _worker_patches() as diarize,
        patch("worker.audio.download_audio", side_effect=download),
        patch("worker.audio.ensure_wav_16k", side_effect=transcode),
    ):
        assert diarization_loop.process_one(_SessionEngine(db_session)) is True

    youtube_id = db_session.execute(text("SELECT youtube_id FROM videos WHERE id=:v"), {"v": video_id}).scalar_one()
    assert seen["url"] == f"https://www.youtube.com/watch?v={youtube_id}"
    assert seen["dest"] == tmp_path / str(video_id)
    assert diarize.call_args.args[0] == tmp_path / str(video_id) / "audio_16k.wav"
    assert not (tmp_path / str(video_id)).exists(), "re-fetched audio must be deleted after the job"
    assert tuple(_state(db_session, video_id)) == ("completed", None)
    labels = db_session.execute(
        text("SELECT speaker_label FROM segments WHERE video_id=:v ORDER BY start_ms"), {"v": video_id}
    ).scalars()
    assert list(labels) == ["Speaker 1", "Speaker 2"]
    centroids = db_session.execute(
        text("""SELECT speaker_label, embedding, speech_seconds, model FROM video_speaker_centroids
                WHERE video_id=:v ORDER BY speaker_label"""),
        {"v": video_id},
    ).all()
    assert [tuple(row) for row in centroids] == [
        ("Speaker 1", [1.0, 0.0], 1.5, "test-model"),
        ("Speaker 2", [0.0, 1.0], 2.5, "test-model"),
    ]


def test_missing_audio_without_refetch_fails_visibly(db_session, monkeypatch):
    video_id = _insert_video(db_session)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_ALLOWED_VIDEO_IDS", frozenset({video_id}))
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_REFETCH_AUDIO", False)

    with _worker_patches() as diarize, patch("worker.audio.download_audio") as download:
        assert diarization_loop.process_one(_SessionEngine(db_session)) is True

    download.assert_not_called()
    diarize.assert_not_called()
    state, error = _state(db_session, video_id)
    assert state == "failed"
    assert error.startswith("WAV file missing for diarization")


def test_failed_refetch_marks_the_video_failed_and_removes_scratch(db_session, monkeypatch, tmp_path):
    video_id = _insert_video(db_session, wav_path=None)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_ALLOWED_VIDEO_IDS", frozenset({video_id}))
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_REFETCH_AUDIO", True)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_REFETCH_DIR", str(tmp_path))

    with _worker_patches() as diarize, patch("worker.audio.download_audio", side_effect=RuntimeError("blocked")):
        assert diarization_loop.process_one(_SessionEngine(db_session)) is True

    diarize.assert_not_called()
    assert tuple(_state(db_session, video_id)) == ("failed", "blocked")
    assert not (tmp_path / str(video_id)).exists()


def test_rediarization_replaces_stored_centroids(db_session, monkeypatch, tmp_path):
    wav = tmp_path / "audio_16k.wav"
    wav.write_bytes(b"wav")
    video_id = _insert_video(db_session, wav_path=str(wav))
    db_session.execute(
        text("""INSERT INTO video_speaker_centroids (video_id, speaker_label, embedding, model)
                VALUES (:v, 'Speaker 9', ARRAY[0.5, 0.5]::real[], 'old')"""),
        {"v": video_id},
    )
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_ALLOWED_VIDEO_IDS", frozenset({video_id}))

    with _worker_patches():
        assert diarization_loop.process_one(_SessionEngine(db_session)) is True

    labels = db_session.execute(
        text("SELECT speaker_label FROM video_speaker_centroids WHERE video_id=:v ORDER BY 1"), {"v": video_id}
    ).scalars()
    assert list(labels) == ["Speaker 1", "Speaker 2"]


def test_backlog_batch_is_capped_and_retries_only_missing_audio_failures(db_session, monkeypatch):
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_MAX_DURATION_SECONDS", 3600)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_BACKLOG_BATCH_SIZE", 3)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_REFETCH_AUDIO", True)
    # Older rows sort first; the test database may hold other rows, so age these far back.
    pending = _insert_video(db_session, created_offset_minutes=10**7 + 5)
    missing_audio = _insert_video(
        db_session,
        diarization_state="failed",
        diarization_error="WAV file missing for diarization: /data/x/audio_16k.wav",
        created_offset_minutes=10**7 + 4,
    )
    other_failure = _insert_video(
        db_session, diarization_state="failed", diarization_error="boom", created_offset_minutes=10**7 + 3
    )
    canary = _insert_video(
        db_session, diarization_error=f"canary-lease:{uuid.uuid4()}", created_offset_minutes=10**7 + 3
    )
    too_long = _insert_video(db_session, duration=3601, created_offset_minutes=10**7 + 3)
    no_wav = _insert_video(db_session, wav_path=None, created_offset_minutes=10**7 + 2)
    beyond_cap = _insert_video(db_session, created_offset_minutes=10**7 + 1)

    batch = diarization_loop._select_backlog_batch(_SessionEngine(db_session))

    assert batch == [pending, missing_audio, no_wav]
    assert tuple(_state(db_session, missing_audio)) == ("pending", None)
    assert _state(db_session, other_failure)[0] == "failed"
    for untouched in (canary, too_long, beyond_cap):
        assert untouched not in batch


def test_backlog_without_refetch_needs_existing_audio_and_never_retries_failures(db_session, monkeypatch):
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_BACKLOG_BATCH_SIZE", 100)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_REFETCH_AUDIO", False)
    missing_audio = _insert_video(
        db_session,
        diarization_state="failed",
        diarization_error="WAV file missing for diarization: /data/x/audio_16k.wav",
    )
    no_wav = _insert_video(db_session, wav_path=None)
    pending = _insert_video(db_session)

    batch = diarization_loop._select_backlog_batch(_SessionEngine(db_session))

    assert pending in batch
    assert missing_audio not in batch and no_wav not in batch
    assert _state(db_session, missing_audio)[0] == "failed"


def test_backlog_mode_rejects_an_explicit_allowlist_or_canary(monkeypatch):
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_BACKLOG_MODE", True)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_ALLOWED_VIDEO_IDS", frozenset({uuid.uuid4()}))
    with pytest.raises(ValueError, match="leave DIARIZATION_ALLOWED_VIDEO_IDS empty"):
        diarization_loop._require_valid_allowlist()
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_ALLOWED_VIDEO_IDS", frozenset())
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_CANARY_MODE", True)
    with pytest.raises(ValueError, match="canary"):
        diarization_loop._require_valid_allowlist()
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_CANARY_MODE", False)
    diarization_loop._require_valid_allowlist()


def test_backlog_run_pins_its_batch_as_the_allowlist_and_exits_when_done(monkeypatch):
    batch = [uuid.uuid4(), uuid.uuid4()]
    monkeypatch.setattr(diarization_loop.settings, "ENABLE_DIARIZATION", True)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_BACKLOG_MODE", True)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_ALLOWED_VIDEO_IDS", frozenset())
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_EXIT_WHEN_IDLE", False)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_MAX_JOBS_PER_PROCESS", 0)
    monkeypatch.setattr(diarization_loop, "_backlog_video_ids", None)
    seen = []

    def process(_engine):
        seen.append(diarization_loop._allowlist_params()["allowed_video_ids"])
        return len(seen) <= len(batch)

    with (
        patch.object(diarization_loop, "create_engine", return_value=Mock()),
        patch.object(diarization_loop, "_select_backlog_batch", return_value=batch),
        patch.object(diarization_loop, "process_one", side_effect=process),
        patch.object(diarization_loop.time, "sleep") as sleep,
    ):
        diarization_loop.run()

    assert seen == [batch, batch, batch]
    sleep.assert_not_called()


def test_empty_backlog_exits_without_processing(monkeypatch):
    monkeypatch.setattr(diarization_loop.settings, "ENABLE_DIARIZATION", True)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_BACKLOG_MODE", True)
    monkeypatch.setattr(diarization_loop.settings, "DIARIZATION_ALLOWED_VIDEO_IDS", frozenset())
    monkeypatch.setattr(diarization_loop, "_backlog_video_ids", None)
    with (
        patch.object(diarization_loop, "create_engine", return_value=Mock()),
        patch.object(diarization_loop, "_select_backlog_batch", return_value=[]),
        patch.object(diarization_loop, "process_one") as process,
    ):
        diarization_loop.run()
    process.assert_not_called()
