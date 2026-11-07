"""Chat messages imported for a recording, served by time range."""

import json
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..db import get_db
from ..exceptions import VideoNotFoundError
from ..schemas import ErrorResponse
from ..settings import settings

MAX_CHAT_LIMIT = 1000


def chat_enabled():
    if not settings.CHAT_REPLAY_ENABLED:
        raise HTTPException(404, "Chat replay is not enabled")


router = APIRouter(prefix="", tags=["Videos"], dependencies=[Depends(chat_enabled)])


class ChatBadge(BaseModel):
    id: str
    version: str


class ChatEmote(BaseModel):
    id: str
    text: str


class ChatMessage(BaseModel):
    offset_ms: int = Field(..., description="Time on the recording's timeline")
    author: str = Field(..., description="Display name as it appears in the imported log")
    text: str
    color: str | None = None
    badges: list[ChatBadge] = Field(default_factory=list)
    emotes: list[ChatEmote] = Field(default_factory=list)


class ChatImportInfo(BaseModel):
    alignment_method: Literal["manual", "audio"]
    alignment_confidence: float | None = Field(
        default=None, description="0-1 for audio alignment; null for a manual offset"
    )
    message_count: int
    imported_at: datetime


class ChatRangeResponse(BaseModel):
    video_id: uuid.UUID
    imported: bool = Field(..., description="False when no chat log has been imported for this recording")
    chat_import: ChatImportInfo | None = None
    start_ms: int
    end_ms: int
    messages: list[ChatMessage] = Field(default_factory=list)
    truncated: bool = Field(default=False, description="More messages exist in the range than were returned")
    next_start_ms: int | None = Field(default=None, description="When truncated, request again from this start_ms")


@router.get(
    "/videos/{video_id}/chat",
    response_model=ChatRangeResponse,
    summary="Get chat messages for a time range",
    description="Imported chat messages with `start_ms <= offset_ms < end_ms`, ordered by time. "
    "Returns at most `limit` messages; a truncated response gives `next_start_ms` to continue from. "
    "Returns 404 while chat replay is disabled.",
    responses={404: {"description": "Chat replay disabled, or video not found", "model": ErrorResponse}},
)
def get_video_chat(
    video_id: uuid.UUID,
    start_ms: int = Query(..., ge=0, le=2**31 - 1),
    end_ms: int = Query(..., ge=1, le=2**31 - 1),
    limit: int = Query(200, ge=1, le=MAX_CHAT_LIMIT),
    db=Depends(get_db),
):
    if end_ms <= start_ms:
        raise HTTPException(422, "end_ms must be after start_ms")
    if not db.execute(text("SELECT 1 FROM videos WHERE id=:id"), {"id": video_id}).first():
        raise VideoNotFoundError(str(video_id))
    response = ChatRangeResponse(video_id=video_id, imported=False, start_ms=start_ms, end_ms=end_ms)
    chat_import = (
        db.execute(
            text("""
                SELECT alignment_method, alignment_confidence, message_count, imported_at
                FROM chat_imports WHERE video_id=:id
            """),
            {"id": video_id},
        )
        .mappings()
        .first()
    )
    if chat_import is None:
        return response
    rows = (
        db.execute(
            text("""
                SELECT offset_ms, author, body, metadata FROM chat_messages
                WHERE video_id=:id AND offset_ms >= :start AND offset_ms < :end
                ORDER BY offset_ms, id LIMIT :limit
            """),
            {"id": video_id, "start": start_ms, "end": end_ms, "limit": limit + 1},
        )
        .mappings()
        .all()
    )
    if len(rows) > limit:
        # Stop before the first omitted millisecond so the next request neither repeats nor skips messages.
        resume = rows[limit]["offset_ms"]
        kept = [row for row in rows[:limit] if row["offset_ms"] < resume]
        if not kept:  # more than `limit` messages in one millisecond
            kept, resume = rows[:limit], resume + 1
        rows, response.truncated, response.next_start_ms = kept, True, resume
    response.imported = True
    response.chat_import = ChatImportInfo(**chat_import)
    response.messages = [_message(row) for row in rows]
    return response


def _message(row) -> ChatMessage:
    metadata = row["metadata"] or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return ChatMessage(offset_ms=row["offset_ms"], author=row["author"], text=row["body"], **metadata)
