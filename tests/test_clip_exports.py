import shutil
import subprocess
import time
import uuid

import pytest
from sqlalchemy import text

from app.clip_exports import ClipError, ClipStore, probe
from app.main import app
from app.security import require_auth
from app.settings import settings


@pytest.fixture
def clip_store(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required for synthetic original checks")
    # Tiny synthetic media can live on a small CI tmpfs; reserve behavior is tested separately.
    monkeypatch.setattr("app.clip_exports.MIN_FREE_BYTES", 0)
    originals = tmp_path / "originals"
    originals.mkdir()
    media = originals / "generated.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-c:a",
            "aac",
            str(media),
        ],
        check=True,
        timeout=20,
    )
    return ClipStore(tmp_path / "spool", originals), media, str(uuid.uuid4()), str(uuid.uuid4())


def register(fixture):
    store, media, video, owner = fixture
    store.register(
        video, owner, media, "Synthetic test pattern generated for export verification", timeline_aligned=True
    )
    return store, media, video, owner


def test_real_render_owner_integrity_and_expiration(clip_store):
    store, media, video, owner = register(clip_store)
    job = store.enqueue(video, owner, 500, 2500)
    assert store.enqueue(video, owner, 500, 2500)["id"] == job["id"]
    with pytest.raises(ClipError):
        store.status(job["id"], str(uuid.uuid4()))
    completed = store.run(job["id"])
    assert completed["status"] == "completed", completed
    output = store.output(job["id"], owner)
    assert abs(probe(output)["duration_ms"] - 2000) < 100
    assert output.stat().st_mode & 0o077 == 0
    completed["expires_at"] = time.time() - 1
    store.save("jobs", job["id"], completed)
    assert store.status(job["id"], owner)["status"] == "expired"
    with pytest.raises(ClipError):
        store.output(job["id"], owner)
    assert store.cleanup()["jobs"] == [job["id"]]
    assert output.exists()
    store.cleanup(apply=True)
    assert not output.exists()


def test_original_boundaries_and_authorization(clip_store, tmp_path):
    store, media, video, owner = clip_store
    outside = tmp_path / "outside.mp4"
    shutil.copyfile(media, outside)
    with pytest.raises(ClipError):
        store.register(video, owner, outside, "Synthetic source", timeline_aligned=True)
    with pytest.raises(ClipError):
        store.register(video, owner, media, "Synthetic source", timeline_aligned=False)
    register(clip_store)
    for start, end in [(-1, 2), (2, 1), (0, 120001), (0, 4000), (True, 1000)]:
        with pytest.raises(ClipError):
            store.enqueue(video, owner, start, end)
    with pytest.raises(ClipError):
        store.enqueue(video, str(uuid.uuid4()), 0, 1000)
    media.unlink()
    media.symlink_to(outside)
    with pytest.raises(ClipError):
        store.enqueue(video, owner, 0, 1000)


def test_changed_original_and_revocation_fail_closed(clip_store):
    store, media, video, owner = register(clip_store)
    job = store.enqueue(video, owner, 0, 1000)
    with media.open("ab") as stream:
        stream.write(b"changed")
    assert store.run(job["id"])["status"] == "failed"
    assert not (store.spool / "jobs" / f"{job['id']}.mp4").exists()
    store.revoke(video)
    with pytest.raises(ClipError):
        store.enqueue(video, owner, 0, 1000)


def test_render_timeout_cleans_partial_output(clip_store, monkeypatch):
    store, media, video, owner = register(clip_store)
    job = store.enqueue(video, owner, 0, 1000)

    def timeout(*args, **kwargs):
        (store.spool / "jobs" / f"{job['id']}.partial.mp4").write_bytes(b"partial")
        raise subprocess.TimeoutExpired("ffmpeg", 180)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert store.run(job["id"])["status"] == "failed"
    assert not list((store.spool / "jobs").glob("*.mp4"))


def test_clip_api_is_owner_only_and_honors_source_deletion(client, db_session, clip_store, monkeypatch):
    store, media, video, owner = register(clip_store)
    monkeypatch.setattr(settings, "CLIP_EXPORTS_ENABLED", True)
    monkeypatch.setattr(settings, "CLIP_ORIGINALS_ROOT", str(store.originals))
    monkeypatch.setattr(settings, "CLIP_SPOOL_DIR", str(store.spool))
    db_session.execute(text("INSERT INTO users(id,name) VALUES (:id,'Original owner')"), {"id": owner})
    job_id = uuid.uuid4()
    db_session.execute(
        text("INSERT INTO jobs(id,kind,input_url) VALUES (:id,'single','https://example.invalid')"), {"id": job_id}
    )
    db_session.execute(
        text("INSERT INTO videos(id,job_id,youtube_id) VALUES (:id,:job,:youtube)"),
        {"id": video, "job": job_id, "youtube": video},
    )
    assert client.post("/clips", json={"video_id": video, "start_ms": 0, "end_ms": 1000}).status_code == 401
    app.dependency_overrides[require_auth] = lambda: {"id": owner, "role": "user"}
    try:
        response = client.post("/clips", json={"video_id": video, "start_ms": 0, "end_ms": 1000})
        assert response.status_code == 202, response.text
        job = response.json()
        assert "owner_id" not in job and "relative_path" not in job
        assert client.get(f"/clips/{job['id']}/download").status_code == 404
        store.run(job["id"])
        download = client.get(f"/clips/{job['id']}/download")
        assert download.status_code == 200
        assert download.headers["content-type"] == "video/mp4"
        assert "no-store" in download.headers["cache-control"]
        app.dependency_overrides[require_auth] = lambda: {"id": str(uuid.uuid4())}
        assert client.get(f"/clips/{job['id']}").status_code == 404
        assert client.get(f"/clips/{job['id']}/download").status_code == 404
        app.dependency_overrides[require_auth] = lambda: {"id": owner, "role": "user"}
        db_session.execute(text("DELETE FROM videos WHERE id=:id"), {"id": video})
        assert client.get(f"/clips/{job['id']}/download").status_code == 404
    finally:
        app.dependency_overrides.pop(require_auth, None)


def test_clip_feature_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "CLIP_EXPORTS_ENABLED", False)
    assert client.get(f"/clips/{uuid.uuid4()}").status_code == 404


@pytest.fixture
def youtube_store(clip_store):
    original_store, media, video, owner = clip_store
    return ClipStore(original_store.spool, source="youtube", ttl_seconds=60), media, video, owner


def fake_ytdlp(monkeypatch, media, *, timeout=False, failure=False):
    """Replace only the network-acquisition process; real ffprobe/ffmpeg still run."""
    from pathlib import Path

    original_popen = subprocess.Popen
    commands = []

    class Download:
        pid = 123456

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait(self, timeout=None):
            if timeout:
                if should_timeout:
                    raise subprocess.TimeoutExpired("yt-dlp", timeout)
                return 1 if failure else 0
            return -9

        def poll(self):
            return None if should_timeout else 0

    should_timeout = timeout

    def start(command, **kwargs):
        if "yt_dlp" not in command:
            return original_popen(command, **kwargs)
        commands.append(command)
        output = Path(command[command.index("--output") + 1].replace("%(ext)s", "mp4"))
        shutil.copyfile(media, output)
        assert kwargs["start_new_session"] is True
        return Download()

    monkeypatch.setattr(subprocess, "Popen", start)
    return commands


def test_youtube_range_command_real_render_and_completion_ttl(youtube_store, monkeypatch):
    store, media, video, owner = youtube_store
    # Fake the selected 3-second download, then exercise actual rendering and probing.
    commands = fake_ytdlp(monkeypatch, media)
    job = store.enqueue(video, owner, 1234500, 1237500, youtube_id="BaW_jenozKc")
    # Simulate time spent queued: completion must grant the entire cache lifetime.
    job["created_at"] -= 30
    job["expires_at"] -= 30
    store.save("jobs", job["id"], job)
    completed = store.run(job["id"])
    assert completed["status"] == "completed", completed
    assert completed["expires_at"] >= completed["completed_at"] + 59
    assert abs(probe(store.output(job["id"], owner))["duration_ms"] - 3000) < 100
    command = commands[0]
    assert command[command.index("--download-sections") + 1] == "*1234.500-1237.500"
    assert command[-1] == "https://www.youtube.com/watch?v=BaW_jenozKc"
    assert "--ignore-config" in command and "--force-keyframes-at-cuts" in command
    assert not list((store.spool / "jobs").glob(f"{job['id']}-*"))
    # Identical requests reuse the download; expiry allows a new generation.
    assert store.enqueue(video, owner, 1234500, 1237500, youtube_id="BaW_jenozKc")["id"] == job["id"]
    completed["expires_at"] = time.time() - 1
    store.save("jobs", job["id"], completed)
    with pytest.raises(ClipError):
        store.output(job["id"], owner)
    store.cleanup(apply=True)
    regenerated = store.enqueue(video, owner, 1234500, 1237500, youtube_id="BaW_jenozKc")
    assert regenerated["id"] != job["id"]
    assert store.run(regenerated["id"])["status"] == "completed"


@pytest.mark.parametrize("value", [None, "https://localhost/secret", "../../other", "bad-id", "x" * 12])
def test_youtube_rejects_arbitrary_urls_and_invalid_ids(youtube_store, value):
    store, media, video, owner = youtube_store
    with pytest.raises(ClipError):
        store.enqueue(video, owner, 0, 1000, youtube_id=value)
    assert not list((store.spool / "jobs").glob("*.json"))


@pytest.mark.parametrize("timeout", [False, True])
def test_youtube_acquisition_failure_cleans_all_temporary_media(youtube_store, monkeypatch, timeout):
    import signal
    from unittest.mock import Mock

    store, media, video, owner = youtube_store
    fake_ytdlp(monkeypatch, media, timeout=timeout, failure=True)
    kill = Mock()
    monkeypatch.setattr("app.clip_exports.os.killpg", kill)
    job = store.enqueue(video, owner, 0, 3000, youtube_id="BaW_jenozKc")
    assert store.run(job["id"])["status"] == "failed"
    assert list((store.spool / "jobs").iterdir()) == [store.record_path("jobs", job["id"])]
    if timeout:
        kill.assert_called_once_with(123456, signal.SIGKILL)
    else:
        kill.assert_not_called()


def test_queue_accepts_requests_while_renderer_is_busy(youtube_store):
    store, media, video, owner = youtube_store
    with store.lock(".render.lock"):
        assert store.enqueue(video, owner, 0, 1000, youtube_id="BaW_jenozKc")["status"] == "queued"
        with pytest.raises(ClipError):
            store.cleanup(apply=True)


def test_saved_range_survives_expiry_and_regenerates(client, db_session, youtube_store, monkeypatch):
    store, media, video, owner = youtube_store
    monkeypatch.setattr(settings, "CLIP_EXPORTS_ENABLED", True)
    monkeypatch.setattr(settings, "CLIP_EXPORT_SOURCE", "youtube")
    monkeypatch.setattr(settings, "CLIP_SPOOL_DIR", str(store.spool))
    monkeypatch.setattr(settings, "CLIP_ORIGINALS_ROOT", "")
    fake_ytdlp(monkeypatch, media)
    db_session.execute(text("INSERT INTO users(id,name) VALUES (:id,'Clip owner')"), {"id": owner})
    ingest_id, favorite_id = str(uuid.uuid4()), str(uuid.uuid4())
    db_session.execute(
        text("INSERT INTO jobs(id,kind,input_url) VALUES (:id,'single','https://example.invalid')"), {"id": ingest_id}
    )
    db_session.execute(
        text("INSERT INTO videos(id,job_id,youtube_id,duration_seconds) VALUES (:id,:job,'BaW_jenozKc',3)"),
        {"id": video, "job": ingest_id},
    )
    db_session.execute(
        text("INSERT INTO favorites(id,user_id,video_id,start_ms,end_ms) VALUES (:id,:owner,:video,0,3000)"),
        {"id": favorite_id, "owner": owner, "video": video},
    )
    route = f"/clips/from-favorite/{favorite_id}"
    assert client.post(route).status_code == 401
    app.dependency_overrides[require_auth] = lambda: {"id": owner, "role": "user"}
    try:
        response = client.post(route)
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        assert client.post(route).json()["id"] == job_id
        completed = store.run(job_id)
        assert completed["status"] == "completed", completed
        assert client.get(f"/clips/{job_id}/download").status_code == 200
        completed["expires_at"] = time.time() - 1
        store.save("jobs", job_id, completed)
        assert client.get(f"/clips/{job_id}/download").status_code == 404
        store.cleanup(apply=True)
        assert (
            db_session.execute(text("SELECT end_ms FROM favorites WHERE id=:id"), {"id": favorite_id}).scalar_one()
            == 3000
        )
        response = client.post(route)
        assert response.status_code == 202 and response.json()["id"] != job_id
        assert client.post("/clips", json={"video_id": video, "start_ms": 0, "end_ms": 4000}).status_code == 422
        app.dependency_overrides[require_auth] = lambda: {"id": str(uuid.uuid4()), "role": "admin"}
        assert client.post(route).status_code == 404
        assert client.get(f"/clips/{response.json()['id']}").status_code == 404
        app.dependency_overrides[require_auth] = lambda: {"id": owner, "role": "user"}
        db_session.execute(text("DELETE FROM videos WHERE id=:id"), {"id": video})
        assert client.post(route).status_code == 404
    finally:
        app.dependency_overrides.pop(require_auth, None)


def test_short_cache_ttl_does_not_expire_a_running_download(youtube_store, monkeypatch):
    store, media, video, owner = youtube_store
    fake_ytdlp(monkeypatch, media)
    acquire = store.acquire_youtube

    def slow_download(job, directory):
        # A one-minute cache must not make the browser abandon a two-minute download.
        now = time.time()
        with monkeypatch.context() as clock:
            clock.setattr(time, "time", lambda: now + 120)
            assert store.status(job["id"], owner)["status"] == "running"
        return acquire(job, directory)

    monkeypatch.setattr(store, "acquire_youtube", slow_download)
    job = store.enqueue(video, owner, 0, 3000, youtube_id="BaW_jenozKc")
    completed = store.run(job["id"])
    assert completed["status"] == "completed"
    assert 59 <= completed["expires_at"] - time.time() <= 60


def test_youtube_download_respects_disk_reserve(youtube_store, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    store, media, video, owner = youtube_store
    reserve = 20 * 1024**3
    monkeypatch.setattr("app.clip_exports.MIN_FREE_BYTES", reserve)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=reserve - 1))
    acquire = Mock()
    monkeypatch.setattr(store, "acquire_youtube", acquire)
    job = store.enqueue(video, owner, 0, 3000, youtube_id="BaW_jenozKc")
    completed = store.run(job["id"])
    assert completed["status"] == "failed" and completed["error"] == "Insufficient spool space"
    acquire.assert_not_called()
