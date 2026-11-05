"""Authenticated, expiring clip exports, including regeneration of saved favorites."""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from ..audit import write_audit_from_request
from ..clip_exports import ClipError, ClipStore
from ..db import get_db
from ..security import require_auth
from ..settings import settings


def store():
    if not settings.CLIP_EXPORTS_ENABLED:
        raise HTTPException(404, "Clip exports are not enabled")
    if not settings.CLIP_SPOOL_DIR or (settings.CLIP_EXPORT_SOURCE == "original" and not settings.CLIP_ORIGINALS_ROOT):
        raise HTTPException(503, "Clip export storage is not configured")
    try:
        return ClipStore(
            Path(settings.CLIP_SPOOL_DIR),
            Path(settings.CLIP_ORIGINALS_ROOT) if settings.CLIP_ORIGINALS_ROOT else None,
            source=settings.CLIP_EXPORT_SOURCE,
            ttl_seconds=settings.CLIP_CACHE_TTL_SECONDS,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "Clip export storage is unavailable") from exc


router = APIRouter(prefix="/clips", tags=["Original clip exports"], dependencies=[Depends(store)])


class ClipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video_id: UUID
    start_ms: int = Field(ge=0, le=2147483647, strict=True)
    end_ms: int = Field(ge=1, le=2147483647, strict=True)


class ClipStatus(BaseModel):
    id: UUID
    video_id: UUID
    start_ms: int
    end_ms: int
    status: str
    expires_at: float
    error: str | None = None


def require_video(db, video_id):
    video = (
        db.execute(text("SELECT id, youtube_id, duration_seconds FROM videos WHERE id=:id"), {"id": str(video_id)})
        .mappings()
        .first()
    )
    if not video:
        raise HTTPException(404, "Source recording not found")
    return video


@router.post("", response_model=ClipStatus, status_code=202)
def request_clip(
    payload: ClipRequest, request: Request, user=Depends(require_auth), db=Depends(get_db), clips=Depends(store)
):
    video = require_video(db, payload.video_id)
    if video["duration_seconds"] is not None and payload.end_ms > video["duration_seconds"] * 1000:
        raise HTTPException(422, "Selection ends after the source recording")
    try:
        job = clips.enqueue(
            str(payload.video_id), str(user["id"]), payload.start_ms, payload.end_ms, youtube_id=video["youtube_id"]
        )
    except (OSError, ClipError) as exc:
        raise HTTPException(422, str(exc) if isinstance(exc, ClipError) else "Source is unavailable") from exc
    write_audit_from_request(
        db, request, "clip.request", user_id=user["id"], resource_type="clip", resource_id=job["id"]
    )
    db.commit()
    return job


@router.post("/from-favorite/{favorite_id}", response_model=ClipStatus, status_code=202)
def export_favorite(
    favorite_id: UUID, request: Request, user=Depends(require_auth), db=Depends(get_db), clips=Depends(store)
):
    """Reuse a cached export or regenerate it from a saved range. The favorite never expires."""
    favorite = (
        db.execute(
            text("SELECT video_id, start_ms, end_ms FROM favorites WHERE id=:id AND user_id=:owner"),
            {"id": str(favorite_id), "owner": str(user["id"])},
        )
        .mappings()
        .first()
    )
    if not favorite:
        raise HTTPException(404, "Saved range not found")
    return request_clip(ClipRequest(**favorite), request, user, db, clips)


def owned_job(job_id, user, db, clips):
    try:
        job = clips.status(str(job_id), str(user["id"]))
        require_video(db, job["video_id"])
        return job
    except (OSError, ClipError) as exc:
        raise HTTPException(404, "Clip unavailable, expired, or not owned by you") from exc


@router.get("/{job_id}", response_model=ClipStatus)
def clip_status(job_id: UUID, user=Depends(require_auth), db=Depends(get_db), clips=Depends(store)):
    return owned_job(job_id, user, db, clips)


@router.get("/{job_id}/download")
def download_clip(job_id: UUID, user=Depends(require_auth), db=Depends(get_db), clips=Depends(store)):
    owned_job(job_id, user, db, clips)
    try:
        output = clips.output(str(job_id), str(user["id"]))
    except (OSError, ClipError) as exc:
        raise HTTPException(404, "Clip is not available for download") from exc
    return FileResponse(
        output, media_type="video/mp4", filename=f"passage-{job_id}.mp4", headers={"Cache-Control": "private, no-store"}
    )
