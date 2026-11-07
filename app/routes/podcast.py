"""Podcasting 2.0 chapters and an RSS feed of completed recordings (opt-in)."""

import json
import re
import uuid
import xml.etree.ElementTree as ET  # nosec B405 - builds XML only; nothing is parsed
from email.utils import format_datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import text

from ..crud import VIDEO_EFFECTIVE_AT_SQL
from ..db import get_db
from ..settings import settings
from .passages import public_origin

router = APIRouter(tags=["Podcast"])

PODCAST_NS = "https://podcastindex.org/namespace/1.0"
CHAPTERS_MEDIA_TYPE = "application/json+chapters"
FEED_LIMIT = 50
ET.register_namespace("podcast", PODCAST_NS)
# Characters XML 1.0 cannot carry even when escaped.
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _require_enabled() -> None:
    if not settings.PODCAST_FEED_ENABLED:
        raise HTTPException(404, "The podcast feed is not enabled")


def _published_chapters(db, video_id) -> list[dict]:
    rows = db.execute(
        text("""
            SELECT start_ms, end_ms, title FROM archive_video_chapters
            WHERE video_id = :video_id AND status = 'published'
            ORDER BY start_ms, chapter_index
        """),
        {"video_id": str(video_id)},
    ).mappings()
    return [dict(row) for row in rows]


@router.get(
    "/videos/{video_id}/podcast-chapters.json",
    summary="Get published chapters in the Podcasting 2.0 JSON chapters format",
    response_class=Response,
    responses={
        200: {"content": {CHAPTERS_MEDIA_TYPE: {}}, "description": "JSON chapters document, version 1.2.0"},
        404: {"description": "Feature disabled, recording missing, or no published chapters"},
    },
)
def get_podcast_chapters(video_id: uuid.UUID, db=Depends(get_db)):
    _require_enabled()
    video = db.execute(text("SELECT title FROM videos WHERE id = :id"), {"id": str(video_id)}).mappings().first()
    if not video:
        raise HTTPException(404, "Recording unavailable")
    chapters = _published_chapters(db, video_id)
    if not chapters:
        raise HTTPException(404, "This recording has no published chapters")
    document: dict = {
        "version": "1.2.0",
        "podcastName": settings.SITE_NAME,
        "chapters": [
            {
                "startTime": row["start_ms"] / 1000,
                "endTime": row["end_ms"] / 1000,
                "title": row["title"] or f"Part {index}",
            }
            for index, row in enumerate(chapters, start=1)
        ],
    }
    if video["title"]:
        document["title"] = video["title"]
    return Response(content=json.dumps(document, ensure_ascii=False), media_type=CHAPTERS_MEDIA_TYPE)


def _sub(parent: ET.Element, tag: str, value: str | None = None, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, tag, {key: _XML_INVALID.sub("", item) for key, item in attrs.items()})
    if value is not None:
        element.text = _XML_INVALID.sub("", value)
    return element


@router.get(
    "/feeds/recordings.rss",
    summary="RSS 2.0 feed of completed recordings with Podcasting 2.0 transcript and chapter links",
    response_class=Response,
    responses={
        200: {"content": {"application/rss+xml": {}}, "description": "RSS 2.0 feed without enclosures"},
        404: {"description": "Feature disabled"},
    },
)
def get_recordings_feed(db=Depends(get_db)):
    _require_enabled()
    origin = public_origin()
    api = f"{origin}/api"
    rows = (
        db.execute(
            text(f"""
                SELECT v.id, v.youtube_id, v.title, v.channel_name, v.language,
                       {VIDEO_EFFECTIVE_AT_SQL} AS published_at,
                       EXISTS (
                           SELECT 1 FROM archive_video_chapters c
                           WHERE c.video_id = v.id AND c.status = 'published'
                       ) AS has_chapters
                FROM videos v
                WHERE v.state = 'completed'
                ORDER BY {VIDEO_EFFECTIVE_AT_SQL} DESC, v.uploaded_at DESC NULLS LAST, v.id
                LIMIT :limit
            """),
            {"limit": FEED_LIMIT},
        )
        .mappings()
        .all()
    )

    rss = ET.Element("rss", {"version": "2.0"})
    channel = _sub(rss, "channel")
    _sub(channel, "title", settings.SITE_NAME)
    _sub(channel, "link", f"{origin}/")
    _sub(channel, "description", settings.SITE_DESCRIPTION)
    for row in rows:
        video_id = str(row["id"])
        link = f"{origin}/v/{video_id}"
        item = _sub(channel, "item")
        _sub(item, "title", row["title"] or row["youtube_id"] or video_id)
        _sub(item, "link", link)
        _sub(item, "guid", video_id, isPermaLink="false")
        if row["published_at"]:
            _sub(item, "pubDate", format_datetime(row["published_at"]))
        if row["channel_name"]:
            _sub(item, "description", f"Transcribed recording from {row['channel_name']}.")
        language = {"language": row["language"]} if row["language"] else {}
        for suffix, media_type in (("vtt", "text/vtt"), ("srt", "application/x-subrip")):
            _sub(
                item,
                f"{{{PODCAST_NS}}}transcript",
                url=f"{api}/videos/{video_id}/transcript.{suffix}",
                type=media_type,
                rel="captions",
                **language,
            )
        if row["has_chapters"]:
            _sub(
                item,
                f"{{{PODCAST_NS}}}chapters",
                url=f"{api}/videos/{video_id}/podcast-chapters.json",
                type=CHAPTERS_MEDIA_TYPE,
            )
    body = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    return Response(content=body, media_type="application/rss+xml")
