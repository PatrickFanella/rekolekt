import shutil
import time
from pathlib import Path
from threading import Event, Thread

from sqlalchemy import create_engine, text

from app.cache import invalidate_video_data
from app.logging_config import configure_logging, get_logger
from app.settings import settings
from worker.diarize import RecordingDiarization, diarize_recording

configure_logging(
    service="diarization-worker",
    level=settings.LOG_LEVEL,
    json_format=(settings.LOG_FORMAT == "json"),
)
logger = get_logger(__name__)

_HEARTBEAT_INTERVAL_SECONDS = 60
_MISSING_WAV_ERROR = "WAV file missing for diarization"

# Backlog mode pins the batch it selected at startup as this process's allow-list.
_backlog_video_ids: list | None = None


def _canary_token() -> str | None:
    token = settings.DIARIZATION_CANARY_TOKEN
    return str(token) if token is not None else None


def _canary_marker() -> str:
    return f"canary-lease:{_canary_token()}"


def _ownership_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    if settings.DIARIZATION_CANARY_MODE:
        return f"{prefix}diarization_error=:canary_marker"
    return f"({prefix}diarization_error IS NULL OR {prefix}diarization_error NOT LIKE 'canary-%')"


def _ownership_params() -> dict:
    return {"canary_marker": _canary_marker()} if settings.DIARIZATION_CANARY_MODE else {}


def _allowed_video_ids():
    if _backlog_video_ids is not None:
        return list(_backlog_video_ids)
    return list(settings.DIARIZATION_ALLOWED_VIDEO_IDS)


def _allowlist_params() -> dict:
    return {"allowed_video_ids": _allowed_video_ids()}


def _require_valid_allowlist() -> None:
    if settings.DIARIZATION_BACKLOG_MODE:
        if settings.DIARIZATION_CANARY_MODE:
            raise ValueError("backlog diarization cannot run in canary mode")
        if settings.DIARIZATION_ALLOWED_VIDEO_IDS:
            raise ValueError("backlog diarization selects its own batch; leave DIARIZATION_ALLOWED_VIDEO_IDS empty")
        return
    allowed = list(settings.DIARIZATION_ALLOWED_VIDEO_IDS)
    if len(allowed) > 5:
        raise ValueError("DIARIZATION_ALLOWED_VIDEO_IDS may contain at most 5 UUIDs")
    if not allowed:
        raise ValueError("standalone diarization worker requires DIARIZATION_ALLOWED_VIDEO_IDS")
    if settings.DIARIZATION_CANARY_MODE and (len(allowed) != 1 or _canary_token() is None):
        raise ValueError("canary diarization requires exactly one allowed UUID and canonical invocation token")


def _load_segments(conn, video_id):
    rows = (
        conn.execute(
            text("""
            SELECT id, start_ms, end_ms, text, speaker_label, confidence, avg_logprob, temperature, token_count
            FROM segments
            WHERE video_id = :v
              AND video_id = ANY(:allowed_video_ids)
            ORDER BY start_ms, id
            """),
            {"v": video_id, **_allowlist_params()},
        )
        .mappings()
        .all()
    )
    return [
        {
            "_segment_id": row["id"],
            "start": float(row["start_ms"] or 0) / 1000.0,
            "end": float(row["end_ms"] or 0) / 1000.0,
            "text": row["text"] or "",
            "speaker": row["speaker_label"],
            "speaker_label": row["speaker_label"],
            "confidence": row["confidence"],
            "avg_logprob": row["avg_logprob"],
            "temperature": row["temperature"],
            "token_count": row["token_count"],
        }
        for row in rows
    ]


def _claim_video(conn):
    if settings.DIARIZATION_CANARY_MODE:
        return (
            conn.execute(
                text("""
                    SELECT v.id, v.wav_path, v.youtube_id FROM videos v
                    WHERE v.id = ANY(:allowed_video_ids)
                      AND v.state = 'completed'
                      AND v.diarization_state='running'
                      AND v.wav_path IS NOT NULL
                      AND v.duration_seconds IS NOT NULL AND v.duration_seconds <= 600
                      AND EXISTS (SELECT 1 FROM segments s WHERE s.video_id=v.id)
                      AND v.diarization_error=:canary_marker
                    FOR UPDATE SKIP LOCKED
                """),
                {**_allowlist_params(), **_ownership_params()},
            )
            .mappings()
            .first()
        )
    row = (
        conn.execute(
            text("""
            SELECT v.id, v.wav_path, v.youtube_id
            FROM videos v
            WHERE v.state = 'completed'
              AND (v.wav_path IS NOT NULL OR :refetch_audio)
              AND v.diarization_state = 'pending'
              AND v.duration_seconds IS NOT NULL
              AND v.duration_seconds <= :max_duration_seconds
              AND v.id = ANY(:allowed_video_ids)
              AND EXISTS (SELECT 1 FROM segments s WHERE s.video_id = v.id)
               AND (v.diarization_error IS NULL OR v.diarization_error NOT LIKE 'canary-%')
            ORDER BY v.updated_at, v.created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """),
            {
                "max_duration_seconds": settings.DIARIZATION_MAX_DURATION_SECONDS,
                "refetch_audio": settings.DIARIZATION_REFETCH_AUDIO,
                **_allowlist_params(),
            },
        )
        .mappings()
        .first()
    )
    if not row:
        return None
    logger.info("Claiming diarization job", extra={"video_id": str(row["id"]), "wav_path": row["wav_path"]})
    result = conn.execute(
        text(
            "UPDATE videos SET diarization_state='running', diarization_error=NULL, updated_at=now() "
            "WHERE id=:v AND id = ANY(:allowed_video_ids) AND " + _ownership_sql()
        ),
        {"v": row["id"], **_allowlist_params(), **_ownership_params()},
    )
    if result.rowcount != 1:
        raise RuntimeError("diarization claim was no longer scoped to one allowed video")
    return row


def _requeue_stale_running(conn):
    rows = conn.execute(
        text("""
            UPDATE videos SET diarization_state='pending',
                diarization_error='Requeued stale running diarization job', updated_at=now()
            WHERE diarization_state='running'
              AND now() - updated_at > (:timeout_minutes * interval '1 minute')
              AND id = ANY(:allowed_video_ids)
              AND (diarization_error IS NULL OR diarization_error NOT LIKE 'canary-%')
            RETURNING id
        """),
        {"timeout_minutes": settings.DIARIZATION_RUNNING_TIMEOUT_MINUTES, **_allowlist_params()},
    ).fetchall()
    if rows:
        logger.warning(
            "Requeued stale running diarization jobs",
            extra={"count": len(rows), "timeout_minutes": settings.DIARIZATION_RUNNING_TIMEOUT_MINUTES},
        )


def _heartbeat_diarization_job(engine, video_id) -> bool:
    """Keep a live diarization claim from being recovered as stale."""
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                UPDATE videos
                SET updated_at=now()
                WHERE id=:v AND diarization_state='running'
                  AND id = ANY(:allowed_video_ids)
                  AND """ + _ownership_sql()),
            {"v": video_id, **_allowlist_params(), **_ownership_params()},
        )
    rowcount = result.rowcount
    if not isinstance(rowcount, int) or rowcount not in (0, 1):
        raise RuntimeError("diarization heartbeat affected more than one video")
    return rowcount == 1


def _run_diarization_heartbeat(engine, video_id, stop_event):
    while not stop_event.wait(_HEARTBEAT_INTERVAL_SECONDS):
        try:
            if not _heartbeat_diarization_job(engine, video_id):
                return
        except Exception:
            logger.warning("Diarization heartbeat failed", extra={"video_id": str(video_id)}, exc_info=True)


def _start_diarization_heartbeat(engine, video_id):
    stop_event = Event()
    thread = Thread(
        target=_run_diarization_heartbeat,
        args=(engine, video_id, stop_event),
        name=f"diarization-heartbeat-{video_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _stop_diarization_heartbeat(stop_event, thread):
    stop_event.set()
    thread.join()


def _skip_unsafe_diarization_jobs(conn):
    """Atomically skip otherwise eligible jobs that could exhaust pyannote memory."""
    rows = conn.execute(
        text("""
            UPDATE videos v
            SET diarization_state = 'skipped',
                diarization_error = 'Skipped diarization: duration_seconds=' ||
                    COALESCE(v.duration_seconds::text, 'NULL') ||
                    ' exceeds DIARIZATION_MAX_DURATION_SECONDS=' || CAST(:max_duration_seconds AS text),
                updated_at = now()
            WHERE v.state = 'completed'
              AND (v.wav_path IS NOT NULL OR :refetch_audio)
              AND EXISTS (SELECT 1 FROM segments s WHERE s.video_id = v.id)
              AND (
                  v.diarization_state = 'pending'
                  OR (
                      v.diarization_state = 'running'
                      AND now() - v.updated_at > (:timeout_minutes * interval '1 minute')
                  )
              )
              AND v.id = ANY(:allowed_video_ids)
              AND (
                  v.duration_seconds IS NULL
                  OR v.duration_seconds > :max_duration_seconds
              )
              AND (v.diarization_error IS NULL OR v.diarization_error NOT LIKE 'canary-%')
            RETURNING v.id, v.duration_seconds
            """),
        {
            "max_duration_seconds": settings.DIARIZATION_MAX_DURATION_SECONDS,
            "timeout_minutes": settings.DIARIZATION_RUNNING_TIMEOUT_MINUTES,
            "refetch_audio": settings.DIARIZATION_REFETCH_AUDIO,
            **_allowlist_params(),
        },
    ).fetchall()
    if rows:
        logger.warning(
            "Skipped diarization jobs exceeding duration limit",
            extra={"count": len(rows), "max_duration_seconds": settings.DIARIZATION_MAX_DURATION_SECONDS},
        )


def _refetch_audio(row, scratch: Path) -> Path:
    """Download and transcode a video's audio again into its scratch directory."""
    from worker.audio import download_audio, ensure_wav_16k

    if not row.get("youtube_id"):
        raise FileNotFoundError(f"{_MISSING_WAV_ERROR}: no source to re-fetch")
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    logger.info("Re-fetching audio for diarization", extra={"video_id": str(row["id"])})
    raw_path = download_audio(f"https://www.youtube.com/watch?v={row['youtube_id']}", scratch)
    wav_path = ensure_wav_16k(raw_path)
    if raw_path != wav_path:
        raw_path.unlink(missing_ok=True)
    return wav_path


def _select_backlog_batch(engine) -> list:
    """Pick at most DIARIZATION_BACKLOG_BATCH_SIZE eligible videos for this run.

    With audio re-fetch enabled, rows that failed only because their WAV was
    deleted are returned to pending; every other failed row stays failed.
    """
    with engine.begin() as conn:
        ids = list(
            conn.execute(
                text("""
                    SELECT v.id FROM videos v
                    WHERE v.state = 'completed'
                      AND v.duration_seconds IS NOT NULL
                      AND v.duration_seconds <= :max_duration_seconds
                      AND (v.wav_path IS NOT NULL OR :refetch_audio)
                      AND (v.diarization_error IS NULL OR v.diarization_error NOT LIKE 'canary-%')
                      AND (
                          v.diarization_state = 'pending'
                          OR (:refetch_audio AND v.diarization_state = 'failed'
                              AND v.diarization_error LIKE :missing_wav_prefix)
                      )
                      AND EXISTS (SELECT 1 FROM segments s WHERE s.video_id = v.id)
                    ORDER BY v.created_at, v.id
                    LIMIT :batch_size
                """),
                {
                    "max_duration_seconds": settings.DIARIZATION_MAX_DURATION_SECONDS,
                    "refetch_audio": settings.DIARIZATION_REFETCH_AUDIO,
                    "missing_wav_prefix": f"{_MISSING_WAV_ERROR}%",
                    "batch_size": settings.DIARIZATION_BACKLOG_BATCH_SIZE,
                },
            ).scalars()
        )
        if ids and settings.DIARIZATION_REFETCH_AUDIO:
            conn.execute(
                text("""
                    UPDATE videos SET diarization_state = 'pending', diarization_error = NULL, updated_at = now()
                    WHERE id = ANY(:ids) AND diarization_state = 'failed' AND diarization_error LIKE :missing_wav_prefix
                """),
                {"ids": ids, "missing_wav_prefix": f"{_MISSING_WAV_ERROR}%"},
            )
    return ids


def _speaker_at(tracks: list[tuple[float, float, str]], moment: float) -> str | None:
    for start, end, label in tracks:
        if start > moment:
            return None
        if start <= moment <= end:
            return label
    return None


def _write_speaker_centroids(conn, video_id, result: RecordingDiarization) -> None:
    """Replace the recording's stored speaker embeddings while it is still owned."""
    conn.execute(text("DELETE FROM video_speaker_centroids WHERE video_id = :v"), {"v": video_id})
    for label, vector in result.centroids.items():
        inserted = conn.execute(
            text("""
                INSERT INTO video_speaker_centroids (video_id, speaker_label, embedding, speech_seconds, model)
                SELECT :v, :label, :embedding, :seconds, :model
                WHERE EXISTS (
                    SELECT 1 FROM videos v
                    WHERE v.id=:v AND v.state='completed' AND v.diarization_state='running'
                      AND v.id = ANY(:allowed_video_ids) AND """ + _ownership_sql("v") + ")"),
            {
                "v": video_id,
                "label": label,
                "embedding": vector,
                "seconds": result.speech_seconds.get(label, 0.0),
                "model": settings.DIARIZATION_MODEL,
                **_allowlist_params(),
                **_ownership_params(),
            },
        )
        if inserted.rowcount != 1:
            raise RuntimeError("diarization centroid write lost ownership of the target video")


def process_one(engine) -> bool:
    with engine.begin() as conn:
        if not settings.DIARIZATION_CANARY_MODE:
            _skip_unsafe_diarization_jobs(conn)
            _requeue_stale_running(conn)
        row = _claim_video(conn)
    if not row:
        return False

    video_id = row["id"]
    wav_path = Path(row["wav_path"]) if row["wav_path"] else None
    logger.info("Starting diarization job", extra={"video_id": str(video_id), "wav_path": str(wav_path)})
    heartbeat_stop, heartbeat_thread = _start_diarization_heartbeat(engine, video_id)
    scratch: Path | None = None

    try:
        if wav_path is None or not wav_path.exists():
            if not settings.DIARIZATION_REFETCH_AUDIO:
                raise FileNotFoundError(f"{_MISSING_WAV_ERROR}: {wav_path}")
            scratch = Path(settings.DIARIZATION_REFETCH_DIR) / str(video_id)
            wav_path = _refetch_audio(row, scratch)
        with engine.begin() as conn:
            segments = _load_segments(conn, video_id)
        logger.info(
            "Loaded transcript segments for diarization",
            extra={"video_id": str(video_id), "segments": len(segments)},
        )
        if not segments:
            if settings.DIARIZATION_CANARY_MODE:
                raise RuntimeError("canary diarization target lost its transcript segments")
            with engine.begin() as conn:
                skipped = conn.execute(
                    text(
                        "UPDATE videos SET diarization_state='skipped', updated_at=now() "
                        "WHERE id=:v AND diarization_state='running' AND id = ANY(:allowed_video_ids) AND "
                        + _ownership_sql()
                    ),
                    {"v": video_id, **_allowlist_params(), **_ownership_params()},
                )
                if skipped.rowcount != 1:
                    raise RuntimeError("diarization empty-segment skip did not affect the running target video")
            return True

        logger.info(
            "Calling pyannote diarization",
            extra={"video_id": str(video_id), "device": settings.DIARIZATION_DEVICE},
        )
        diarization = diarize_recording(wav_path)
        logger.info(
            "pyannote diarization returned",
            extra={
                "video_id": str(video_id),
                "speaker_regions": len(diarization.tracks),
                "speakers": len(diarization.speech_seconds),
            },
        )
        with engine.begin() as conn:
            # Keep label writeback and the final durable token transition atomic.
            owned = (
                conn.execute(
                    text("""
                    SELECT id FROM videos WHERE id=:v AND state='completed' AND diarization_state='running'
                      AND id = ANY(:allowed_video_ids) AND diarization_error=:canary_marker
                    FOR UPDATE
                """),
                    {"v": video_id, **_allowlist_params(), **_ownership_params()},
                ).first()
                if settings.DIARIZATION_CANARY_MODE
                else True
            )
            if not owned:
                raise RuntimeError("diarization canary fencing token no longer owns target")
            assigned = 0
            for seg in segments:
                speaker = _speaker_at(diarization.tracks, (seg["start"] + seg["end"]) / 2.0)
                if speaker:
                    assigned += 1
                result = conn.execute(
                    text(
                        """
                        UPDATE segments SET speaker_label=:spk
                        WHERE id=:sid AND video_id=:v AND video_id = ANY(:allowed_video_ids)
                          AND EXISTS (
                              SELECT 1 FROM videos v
                              WHERE v.id=:v AND v.state='completed' AND v.diarization_state='running' AND """
                        + _ownership_sql("v")
                        + ")"
                    ),
                    {
                        "sid": seg["_segment_id"],
                        "v": video_id,
                        "spk": speaker,
                        **_allowlist_params(),
                        **_ownership_params(),
                    },
                )
                if result.rowcount != 1:
                    raise RuntimeError("diarization segment update did not affect exactly one target segment")
            if assigned == 0:
                raise RuntimeError("Diarization completed without assigning any speaker labels")
            _write_speaker_centroids(conn, video_id, diarization)
            completed = conn.execute(
                text("""
                    UPDATE videos
                    SET diarization_state='completed', diarization_error=CASE WHEN :canary_mode THEN 'canary-finalizing:' || :canary_token ELSE NULL END, updated_at=now()
                    WHERE id=:v AND state='completed' AND diarization_state='running' AND id = ANY(:allowed_video_ids)
                      AND """ + _ownership_sql()),
                {
                    "v": video_id,
                    "canary_mode": settings.DIARIZATION_CANARY_MODE,
                    "canary_token": _canary_token(),
                    **_allowlist_params(),
                    **_ownership_params(),
                },
            )
            if completed.rowcount != 1:
                raise RuntimeError("diarization completion did not affect the running target video")
        logger.info(
            "Diarization job completed",
            extra={"video_id": str(video_id), "speakers_assigned": assigned},
        )
        invalidate_video_data(video_id)
        return True
    except Exception as e:
        logger.exception("Diarization job failed", extra={"video_id": str(video_id), "error": str(e)})
        with engine.begin() as conn:
            failed = conn.execute(
                text("""
                    UPDATE videos
                    SET diarization_state='failed', diarization_error=CASE WHEN :canary_mode THEN 'canary-failed:' || :canary_token ELSE :e END, updated_at=now()
                    WHERE id=:v AND diarization_state='running' AND id = ANY(:allowed_video_ids)
                      AND """ + _ownership_sql()),
                {
                    "v": video_id,
                    "e": str(e)[:5000],
                    "canary_mode": settings.DIARIZATION_CANARY_MODE,
                    "canary_token": _canary_token(),
                    **_allowlist_params(),
                    **_ownership_params(),
                },
            )
            if failed.rowcount != 1:
                raise RuntimeError("diarization failure did not affect the running target video")
        return True
    finally:
        _stop_diarization_heartbeat(heartbeat_stop, heartbeat_thread)
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def run():
    global _backlog_video_ids
    _require_valid_allowlist()
    if not settings.ENABLE_DIARIZATION:
        logger.warning("ENABLE_DIARIZATION is false; diarization worker will idle")
    engine = create_engine(settings.DATABASE_URL, future=True, pool_pre_ping=True, hide_parameters=True)
    if settings.DIARIZATION_BACKLOG_MODE:
        _backlog_video_ids = _select_backlog_batch(engine)
        logger.info("Selected diarization backlog batch", extra={"videos": len(_backlog_video_ids)})
        if not _backlog_video_ids:
            return
    logger.info(
        "Diarization worker started",
        extra={"device": settings.DIARIZATION_DEVICE, "inline": settings.DIARIZATION_INLINE},
    )
    processed_attempts = 0
    while True:
        try:
            did_work = process_one(engine) if settings.ENABLE_DIARIZATION else False
        except Exception as e:
            logger.exception("Diarization worker loop failed", extra={"error": str(e)})
            did_work = False
        if did_work:
            processed_attempts += 1
            if (
                settings.DIARIZATION_MAX_JOBS_PER_PROCESS > 0
                and processed_attempts >= settings.DIARIZATION_MAX_JOBS_PER_PROCESS
            ):
                logger.info(
                    "Diarization worker reached process job limit; exiting for restart",
                    extra={
                        "processed_attempts": processed_attempts,
                        "max_jobs_per_process": settings.DIARIZATION_MAX_JOBS_PER_PROCESS,
                    },
                )
                return
        if not did_work:
            if settings.DIARIZATION_EXIT_WHEN_IDLE or settings.DIARIZATION_BACKLOG_MODE:
                logger.info("Diarization worker is idle; exiting by configuration")
                return
            time.sleep(settings.DIARIZATION_POLL_INTERVAL)


def main():
    run()


if __name__ == "__main__":
    main()
