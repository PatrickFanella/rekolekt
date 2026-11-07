"""Database maintenance performed between worker queue claims."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from worker.state_model import VideoState


@dataclass(frozen=True)
class ModelUpgradePolicy:
    """The engine every completed native transcript should come from.

    Model names are compared as strings, so any name works, including ones this
    code has never seen. Backend and version are only compared when set.
    """

    target_model: str
    target_backend: str | None = None
    target_version: str | None = None

    def matches(self, engine: dict[str, Any]) -> bool:
        return (
            engine.get("model") == self.target_model
            and (self.target_backend is None or engine.get("backend") == self.target_backend)
            and (self.target_version is None or engine.get("version") == self.target_version)
        )


def model_upgrade_policy(settings: Any) -> ModelUpgradePolicy | None:
    """Build the policy from settings; an empty target model disables the requeue."""
    target_model = (getattr(settings, "MODEL_UPGRADE_TARGET_MODEL", "") or "").strip()
    if not target_model:
        return None
    return ModelUpgradePolicy(
        target_model=target_model,
        target_backend=(getattr(settings, "MODEL_UPGRADE_TARGET_BACKEND", "") or "").strip() or None,
        target_version=(getattr(settings, "MODEL_UPGRADE_TARGET_VERSION", "") or "").strip() or None,
    )


def rescue_stuck_videos(conn, *, after_seconds: int) -> None:
    if after_seconds <= 0:
        return
    conn.execute(
        text("""
            UPDATE videos
            SET state = 'pending', updated_at = now()
            WHERE state IN ('downloading','transcoding','transcribing')
              AND (diarization_error IS NULL OR diarization_error NOT LIKE 'canary-%')
              AND now() - updated_at > make_interval(secs => :secs)
            RETURNING id
        """),
        {"secs": after_seconds},
    )


def requeue_for_model_upgrade(conn, *, policy: ModelUpgradePolicy, produced_before: datetime, limit: int) -> list:
    """Requeue up to ``limit`` completed videos whose transcript did not come from the target engine.

    Rows from before provenance was recorded fall back to the legacy ``model``
    column for the model check; their unknown backend and version count as a
    mismatch when the policy names one. Only transcripts created before
    ``produced_before`` are eligible, so a transcript this worker just produced
    with a fallback engine is not requeued again in a loop.
    """
    if limit <= 0:
        return []
    mismatch = ["COALESCE(t.engine_model, t.model) IS DISTINCT FROM :target_model"]
    params: dict[str, Any] = {
        "target_model": policy.target_model,
        "completed_state": VideoState.COMPLETED.value,
        "produced_before": produced_before,
        "limit": limit,
    }
    if policy.target_backend is not None:
        mismatch.append("t.engine_backend IS DISTINCT FROM :target_backend")
        params["target_backend"] = policy.target_backend
    if policy.target_version is not None:
        mismatch.append("t.engine_version IS DISTINCT FROM :target_version")
        params["target_version"] = policy.target_version
    result = conn.execute(
        text(f"""
            WITH picked AS (
                SELECT v.id, COALESCE(t.engine_model, t.model) AS model
                FROM videos v
                JOIN transcripts t ON t.video_id = v.id
                WHERE v.state = :completed_state
                  AND (v.diarization_error IS NULL OR v.diarization_error NOT LIKE 'canary-%')
                  AND t.created_at < :produced_before
                  AND ({" OR ".join(mismatch)})
                ORDER BY t.created_at, v.id
                LIMIT :limit
                FOR UPDATE OF v SKIP LOCKED
            )
            UPDATE videos v
            SET state = 'pending', updated_at = now()
            FROM picked
            WHERE v.id = picked.id
            RETURNING v.id, picked.model
        """),
        params,
    )
    return list(result.fetchall())
