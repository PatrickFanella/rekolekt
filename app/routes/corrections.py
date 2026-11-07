"""Transcript corrections from signed-in users, reviewed by moderators.

Registered only when TRANSCRIPT_CORRECTIONS_ENABLED is set (see app.main).
"""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from .. import transcript_corrections as corrections
from ..audit import write_audit_from_request
from ..cache import invalidate_video_data
from ..db import get_db
from ..security import require_auth
from .community import Reason, moderate_user

router = APIRouter(tags=["Transcript corrections"])

MAX_CORRECTION_CHARS = 2000
PAGE = 100


class CorrectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    video_id: UUID
    source: Literal["whisper", "youtube"]
    start_ms: int = Field(ge=0, le=2147483647, strict=True)
    end_ms: int = Field(ge=0, le=2147483647, strict=True)
    text: str = Field(min_length=1, max_length=MAX_CORRECTION_CHARS)

    @model_validator(mode="after")
    def validate_range(self):
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must not precede start_ms")
        return self


class Correction(BaseModel):
    id: UUID
    video_id: UUID
    source: str
    start_ms: int
    end_ms: int
    machine_text: str
    corrected_text: str
    status: Literal["pending", "accepted", "rejected", "stale", "superseded"]
    review_reason: str | None
    created_at: datetime
    reviewed_at: datetime | None


class QueuedCorrection(Correction):
    supporters: int = Field(description="Distinct accounts proposing this same text for this pending segment")


class CorrectionPage(BaseModel):
    items: list[Correction]
    next_offset: int | None


class QueuedCorrectionPage(BaseModel):
    items: list[QueuedCorrection]
    next_offset: int | None


class PublicCorrection(BaseModel):
    id: UUID
    source: str
    start_ms: int
    end_ms: int
    machine_text: str
    corrected_text: str
    accepted_at: datetime


class PublicCorrectionPage(BaseModel):
    items: list[PublicCorrection]
    next_offset: int | None


SELECT = """SELECT c.id, c.video_id, c.source, c.start_ms, c.end_ms, c.original_text AS machine_text,
    c.corrected_text, c.status, c.review_reason, c.created_at, c.reviewed_at FROM segment_corrections c """


def page(db, sql: str, params: dict, offset: int, limit: int = PAGE) -> dict:
    rows = db.execute(text(sql + " LIMIT :limit OFFSET :offset"), params | {"limit": limit + 1, "offset": offset})
    items = [dict(row) for row in rows.mappings()]
    return {"items": items[:limit], "next_offset": offset + limit if len(items) > limit else None}


def read_correction(db, correction_id) -> dict:
    row = db.execute(text(SELECT + "WHERE c.id=:id"), {"id": correction_id}).mappings().first()
    if not row:
        raise HTTPException(404, "Correction not found")
    return dict(row)


def audit(db, request, user, action, correction, details=None):
    write_audit_from_request(
        db,
        request,
        f"transcript_correction.{action}",
        user_id=user["id"],
        resource_type="segment_correction",
        resource_id=str(correction["id"]),
        details={"video_id": str(correction["video_id"]), **(details or {})},
    )


@router.post("/corrections", response_model=Correction, status_code=201)
def propose_correction(payload: CorrectionInput, request: Request, user=Depends(require_auth), db=Depends(get_db)):
    # Serialize per-account submissions, including concurrent requests.
    db.execute(text("SELECT id FROM users WHERE id=:id FOR UPDATE"), {"id": user["id"]})
    count = db.execute(
        text(
            "SELECT count(*) FROM segment_corrections WHERE proposer_id=:id AND created_at > now()-interval '1 minute'"
        ),
        {"id": user["id"]},
    ).scalar()
    if count >= 10:
        raise HTTPException(429, "Please wait a minute before proposing another correction")
    if not db.execute(text("SELECT 1 FROM videos WHERE id=:id"), {"id": payload.video_id}).first():
        raise HTTPException(404, "Source video not found")
    key = (payload.video_id, payload.source, payload.start_ms, payload.end_ms)
    current = corrections.segment_text(db, *key)
    if current is None:
        raise HTTPException(404, "No transcript segment has that source and time range")
    if payload.text == current.strip():
        raise HTTPException(422, "The proposed text matches the current transcript")
    machine = corrections.machine_text(db, *key, current)
    correction_id = uuid4()
    db.execute(
        text("""INSERT INTO segment_corrections
        (id, video_id, source, start_ms, end_ms, original_sha256, original_text, corrected_text, proposer_id)
        VALUES (:id, :video, :source, :start, :end, :digest, :machine, :corrected, :proposer)"""),
        {
            "id": correction_id,
            "video": payload.video_id,
            "source": payload.source,
            "start": payload.start_ms,
            "end": payload.end_ms,
            "digest": corrections.digest(machine),
            "machine": machine,
            "corrected": payload.text,
            "proposer": user["id"],
        },
    )
    result = read_correction(db, correction_id)
    audit(db, request, user, "propose", result)
    db.commit()
    return result


@router.get("/corrections/mine", response_model=CorrectionPage)
def my_corrections(offset: int = Query(0, ge=0, le=100000), user=Depends(require_auth), db=Depends(get_db)):
    """The caller's own proposals and their outcomes, including review reasons."""
    return page(
        db, SELECT + "WHERE c.proposer_id=:user ORDER BY c.created_at DESC, c.id DESC", {"user": user["id"]}, offset
    )


@router.get("/corrections/queue", response_model=QueuedCorrectionPage)
def correction_queue(
    status: Literal["pending", "stale"] = "pending",
    offset: int = Query(0, ge=0, le=100000),
    user=Depends(moderate_user),
    db=Depends(get_db),
):
    """Proposals awaiting review, or accepted corrections a re-transcription left stale."""
    supporters = """, (SELECT count(DISTINCT o.proposer_id) FROM segment_corrections o
        WHERE o.status='pending' AND o.video_id=c.video_id AND o.source=c.source AND o.start_ms=c.start_ms
          AND o.end_ms=c.end_ms AND o.corrected_text=c.corrected_text) AS supporters """
    sql = SELECT.replace(" FROM segment_corrections c ", supporters + "FROM segment_corrections c ")
    return page(db, sql + "WHERE c.status=:status ORDER BY c.created_at, c.id", {"status": status}, offset)


@router.post("/corrections/{correction_id}/accept", response_model=Correction)
def accept_correction(
    correction_id: UUID, payload: Reason, request: Request, user=Depends(moderate_user), db=Depends(get_db)
):
    row = (
        db.execute(text("SELECT * FROM segment_corrections WHERE id=:id FOR UPDATE"), {"id": correction_id})
        .mappings()
        .first()
    )
    if not row:
        raise HTTPException(404, "Correction not found")
    if row["status"] != "pending":
        raise HTTPException(409, "Only pending corrections can be accepted")
    try:
        corrections.accept_correction(db, dict(row), reviewer_id=user["id"], reason=payload.reason)
    except corrections.CorrectionConflictError:
        audit(db, request, user, "stale", row, {"reason": payload.reason})
        db.commit()
        raise HTTPException(409, "The transcript changed since this was proposed; it is now listed as stale")
    result = read_correction(db, correction_id)
    audit(db, request, user, "accept", result, {"reason": payload.reason})
    db.commit()
    invalidate_video_data(result["video_id"])
    return result


@router.post("/corrections/{correction_id}/reject", response_model=Correction)
def reject_correction(
    correction_id: UUID, payload: Reason, request: Request, user=Depends(moderate_user), db=Depends(get_db)
):
    """Reject a pending proposal or dismiss a stale correction. Neither changes transcript text."""
    updated = db.execute(
        text("""UPDATE segment_corrections
        SET status='rejected', reviewer_id=:reviewer, review_reason=:reason, reviewed_at=now(), updated_at=now()
        WHERE id=:id AND status IN ('pending','stale') RETURNING id"""),
        {"id": correction_id, "reviewer": user["id"], "reason": payload.reason},
    ).first()
    if not updated:
        read_correction(db, correction_id)
        raise HTTPException(409, "Only pending or stale corrections can be rejected")
    result = read_correction(db, correction_id)
    audit(db, request, user, "reject", result, {"reason": payload.reason})
    db.commit()
    return result


@router.get("/videos/{video_id}/corrections", response_model=PublicCorrectionPage)
def video_corrections(video_id: UUID, offset: int = Query(0, ge=0, le=100000), db=Depends(get_db)):
    """Accepted corrections currently applied to this recording's transcripts."""
    return page(
        db,
        """SELECT id, source, start_ms, end_ms, original_text AS machine_text, corrected_text, accepted_at
        FROM segment_corrections WHERE video_id=:video AND status='accepted' ORDER BY source, start_ms, end_ms""",
        {"video": video_id},
        offset,
    )
