"""Administrator attribution of diarized speaker labels to archive people."""

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import text

from .. import speakers
from ..audit import ACTION_ADMIN_ACTION, write_audit_event
from ..cache import invalidate_video_data
from ..db import get_db
from ..exceptions import VideoNotFoundError
from ..schemas import (
    SpeakerMappingResult,
    SpeakerMappingUpdate,
    SpeakerPerson,
    SpeakerVoiceReference,
    SpeakerVoiceReferenceCreate,
    SpeakerVoiceReferenceListResponse,
    VideoSpeakerAdmin,
    VideoSpeakerAdminListResponse,
)
from ..security import ROLE_ADMIN, require_role

router = APIRouter(prefix="", tags=["Speakers"])


def _require_video(db, video_id: uuid.UUID) -> None:
    if db.execute(text("SELECT 1 FROM videos WHERE id = :v"), {"v": str(video_id)}).first() is None:
        raise VideoNotFoundError(str(video_id))


def _audit(db, request: Request, user: dict, operation: str, resource_id: str, details: dict) -> None:
    write_audit_event(
        db,
        ACTION_ADMIN_ACTION,
        user_id=user["id"],
        resource_type="video_speaker",
        resource_id=resource_id,
        details={"operation": operation, **details},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.get(
    "/admin/videos/{video_id}/speakers",
    response_model=VideoSpeakerAdminListResponse,
    summary="List a recording's speaker labels (Admin)",
    description="Distinct diarized labels with segment counts, sample segments, the current mapping and the best "
    "enrolled-voice suggestion.",
)
def admin_list_video_speakers(
    video_id: uuid.UUID,
    db=Depends(get_db),
    user=Depends(require_role(ROLE_ADMIN)),
):
    _require_video(db, video_id)
    return VideoSpeakerAdminListResponse(
        video_id=video_id,
        items=[VideoSpeakerAdmin.model_validate(item) for item in speakers.list_video_speakers(db, video_id)],
    )


@router.put(
    "/admin/videos/{video_id}/speakers/{speaker_label}",
    response_model=SpeakerMappingResult,
    summary="Map a speaker label to an archive person (Admin)",
)
def admin_set_video_speaker(
    video_id: uuid.UUID,
    speaker_label: str,
    payload: SpeakerMappingUpdate,
    request: Request,
    db=Depends(get_db),
    user=Depends(require_role(ROLE_ADMIN)),
):
    _require_video(db, video_id)
    result = speakers.set_speaker_person(db, video_id, speaker_label, payload.person_slug, user_id=user["id"])
    reference_id = None
    if payload.enroll:
        segment_ids = [sample["id"] for sample in _label_samples(db, video_id, speaker_label)]
        reference_id = str(
            speakers.add_voice_reference(
                db, payload.person_slug, video_id, speaker_label, segment_ids, user_id=user["id"]
            )["id"]
        )
    _audit(
        db,
        request,
        user,
        "speaker_mapping_set",
        f"{video_id}:{speaker_label}",
        {"person_slug": payload.person_slug, "enrolled_reference_id": reference_id},
    )
    db.commit()
    invalidate_video_data(video_id)
    person = result["person"]
    return SpeakerMappingResult(
        video_id=video_id,
        speaker_label=speaker_label,
        person=SpeakerPerson(slug=person["slug"], display_name=person["display_name"]),
        reindexed_segments=result["reindexed"],
    )


def _label_samples(db, video_id: uuid.UUID, speaker_label: str) -> list[dict]:
    for item in speakers.list_video_speakers(db, video_id):
        if item["speaker_label"] == speaker_label:
            return list(item["samples"])
    return []


@router.delete(
    "/admin/videos/{video_id}/speakers/{speaker_label}",
    response_model=SpeakerMappingResult,
    summary="Clear a speaker label's person (Admin)",
    description="Records that the label is no known person. Automatic matching leaves cleared labels alone.",
)
def admin_clear_video_speaker(
    video_id: uuid.UUID,
    speaker_label: str,
    request: Request,
    db=Depends(get_db),
    user=Depends(require_role(ROLE_ADMIN)),
):
    _require_video(db, video_id)
    reindexed = speakers.clear_speaker_person(db, video_id, speaker_label, user_id=user["id"])
    _audit(db, request, user, "speaker_mapping_cleared", f"{video_id}:{speaker_label}", {})
    db.commit()
    invalidate_video_data(video_id)
    return SpeakerMappingResult(video_id=video_id, speaker_label=speaker_label, reindexed_segments=reindexed)


@router.get(
    "/admin/archive/metadata/people/{slug}/voice-references",
    response_model=SpeakerVoiceReferenceListResponse,
    summary="List a person's enrolled voices (Admin)",
)
def admin_list_voice_references(
    slug: str,
    db=Depends(get_db),
    user=Depends(require_role(ROLE_ADMIN)),
):
    return SpeakerVoiceReferenceListResponse(
        person_slug=slug,
        items=[SpeakerVoiceReference.model_validate(row) for row in speakers.list_voice_references(db, slug)],
    )


@router.post(
    "/admin/archive/metadata/people/{slug}/voice-references",
    response_model=SpeakerVoiceReference,
    status_code=status.HTTP_201_CREATED,
    summary="Enroll a confirmed voice for a person (Admin)",
    description="Mark segments of one recording's speaker label as this person. The label's stored voice "
    "embedding becomes a reference for automatic matching.",
)
def admin_add_voice_reference(
    slug: str,
    payload: SpeakerVoiceReferenceCreate,
    request: Request,
    db=Depends(get_db),
    user=Depends(require_role(ROLE_ADMIN)),
):
    _require_video(db, payload.video_id)
    reference = speakers.add_voice_reference(
        db, slug, payload.video_id, payload.speaker_label, payload.segment_ids, user_id=user["id"]
    )
    _audit(
        db,
        request,
        user,
        "speaker_reference_added",
        str(reference["id"]),
        {"person_slug": slug, "video_id": str(payload.video_id), "speaker_label": payload.speaker_label},
    )
    db.commit()
    return SpeakerVoiceReference.model_validate(reference)


@router.delete(
    "/admin/archive/metadata/people/{slug}/voice-references/{reference_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an enrolled voice (Admin)",
)
def admin_delete_voice_reference(
    slug: str,
    reference_id: uuid.UUID,
    request: Request,
    db=Depends(get_db),
    user=Depends(require_role(ROLE_ADMIN)),
):
    speakers.delete_voice_reference(db, slug, reference_id)
    _audit(db, request, user, "speaker_reference_deleted", str(reference_id), {"person_slug": slug})
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
