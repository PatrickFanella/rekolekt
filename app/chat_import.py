"""Parse an operator-supplied chat log and store it on a recording's timeline.

A chat log is timed against the original broadcast ("source time"). An
alignment is a list of segments mapping source time to recording time, so an
edited re-upload with cuts maps piecewise. Messages outside every segment, or
after the end of the recording, are dropped.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

FORMATS = ("twitchdownloader", "rustlog")
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_MESSAGES = 2_000_000
MAX_SEGMENTS = 1000
MAX_AUTHOR_CHARS = 64
MAX_BODY_CHARS = 1000
MAX_BADGES = 10
MAX_EMOTES = 20
RUSTLOG_PRIVMSG = 1
DEFAULT_MIN_CONFIDENCE = 0.6
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_FRACTION = re.compile(r"\.(\d{6})\d+")


class ChatImportError(ValueError):
    """The chat log, alignment or target recording is not acceptable."""


@dataclass(frozen=True)
class Segment:
    """Source times in ``[source_start_ms, source_end_ms)`` start at ``recording_start_ms``."""

    source_start_ms: int
    source_end_ms: int | None
    recording_start_ms: int

    def as_dict(self) -> dict:
        return {
            "source_start_ms": self.source_start_ms,
            "source_end_ms": self.source_end_ms,
            "recording_start_ms": self.recording_start_ms,
        }


@dataclass(frozen=True)
class Alignment:
    method: str
    segments: tuple[Segment, ...]
    confidence: float | None = None

    def map(self, source_ms: int) -> int | None:
        for segment in self.segments:
            if source_ms >= segment.source_start_ms and (
                segment.source_end_ms is None or source_ms < segment.source_end_ms
            ):
                return segment.recording_start_ms + source_ms - segment.source_start_ms
        return None


def manual_alignment(offset_ms: int) -> Alignment:
    """Recording time = source time + ``offset_ms``; negative when the recording starts later."""
    start = max(0, -offset_ms)
    return Alignment("manual", (Segment(start, None, start + offset_ms),))


def validate_alignment(alignment: Alignment) -> None:
    if alignment.method not in ("manual", "audio"):
        raise ChatImportError("unknown alignment method")
    if not 1 <= len(alignment.segments) <= MAX_SEGMENTS:
        raise ChatImportError(f"an alignment needs 1 to {MAX_SEGMENTS} segments")
    for segment in alignment.segments:
        if segment.source_start_ms < 0 or segment.recording_start_ms < 0:
            raise ChatImportError("alignment segments must start at non-negative times")
        if segment.source_end_ms is not None and segment.source_end_ms <= segment.source_start_ms:
            raise ChatImportError("alignment segments must have positive length")
    if (alignment.method == "manual") != (alignment.confidence is None):
        raise ChatImportError("audio alignments carry a confidence; manual offsets do not")


@dataclass(frozen=True)
class ChatMessage:
    source_ms: int
    author: str
    body: str
    metadata: dict | None = None


@dataclass
class ChatLog:
    source_format: str
    source_name: str
    digest: str
    messages: list[ChatMessage]
    broadcast_started_at: datetime | None = None
    skipped: dict[str, int] = field(default_factory=dict)


def read_chat_log(
    path: Path,
    source_format: str,
    *,
    broadcast_start: datetime | None = None,
    max_bytes: int = MAX_FILE_BYTES,
) -> ChatLog:
    """Read and validate one chat log file; raise ``ChatImportError`` with the reason."""
    if source_format not in FORMATS:
        raise ChatImportError(f"format must be one of {', '.join(FORMATS)}")
    if (source_format == "rustlog") != (broadcast_start is not None):
        raise ChatImportError("rustlog exports need --broadcast-start; TwitchDownloader files must not set it")
    size = path.stat().st_size
    if size > max_bytes:
        raise ChatImportError(f"chat log is {size} bytes; the limit is {max_bytes}")
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ChatImportError(f"chat log exceeds {max_bytes} bytes")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChatImportError(f"chat log is not valid JSON: {exc}") from None
    log = ChatLog(source_format, path.name[:255], hashlib.sha256(raw).hexdigest(), [])
    del raw
    if broadcast_start is None:
        _parse_twitchdownloader(document, log)
    else:
        if broadcast_start.tzinfo is None:
            raise ChatImportError("--broadcast-start needs a UTC offset, for example 2026-11-07T18:02:11Z")
        log.broadcast_started_at = broadcast_start
        _parse_rustlog(document, broadcast_start, log)
    if not log.messages:
        raise ChatImportError("chat log contains no chat messages")
    return log


def _items(document, key: str) -> list:
    items = document.get(key) if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise ChatImportError(f"chat log has no '{key}' list; is --format right?")
    if len(items) > MAX_MESSAGES:
        raise ChatImportError(f"chat log has {len(items)} entries; the limit is {MAX_MESSAGES}")
    return items


def _message(index: int, source_ms: int, author, body, metadata: dict) -> ChatMessage:
    if not isinstance(author, str) or not 1 <= len(author.strip()) <= MAX_AUTHOR_CHARS:
        raise ChatImportError(f"message {index}: author must be 1-{MAX_AUTHOR_CHARS} characters")
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY_CHARS:
        raise ChatImportError(f"message {index}: text must be 1-{MAX_BODY_CHARS} characters")
    return ChatMessage(
        source_ms, author.strip(), body, {key: value for key, value in metadata.items() if value} or None
    )


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _color(value) -> str | None:
    return value if isinstance(value, str) and _COLOR.match(value) else None


def _parse_twitchdownloader(document, log: ChatLog) -> None:
    for index, comment in enumerate(_items(document, "comments")):
        if not isinstance(comment, dict):
            raise ChatImportError(f"message {index}: expected an object")
        offset = comment.get("content_offset_seconds")
        if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not 0 <= offset < 2**31 / 1000:
            raise ChatImportError(f"message {index}: content_offset_seconds must be a non-negative number")
        commenter, message = _object(comment.get("commenter")), _object(comment.get("message"))
        badges = [
            {"id": str(badge["_id"])[:50], "version": str(badge.get("version", ""))[:50]}
            for badge in message.get("user_badges") or []
            if isinstance(badge, dict) and badge.get("_id")
        ][:MAX_BADGES]
        emotes: dict[str, str] = {}
        for fragment in message.get("fragments") or []:
            emoticon = fragment.get("emoticon") if isinstance(fragment, dict) else None
            if isinstance(emoticon, dict) and emoticon.get("emoticon_id") and isinstance(fragment.get("text"), str):
                emotes.setdefault(str(emoticon["emoticon_id"])[:50], fragment["text"][:100])
        metadata = {
            "color": _color(message.get("user_color")),
            "badges": badges,
            "emotes": [{"id": key, "text": value} for key, value in list(emotes.items())[:MAX_EMOTES]],
        }
        author = commenter.get("display_name") or commenter.get("name")
        log.messages.append(_message(index, round(offset * 1000), author, message.get("body"), metadata))


def _timestamp(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(_FRACTION.sub(r".\1", value.replace("Z", "+00:00")))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed


def _parse_rustlog(document, broadcast_start: datetime, log: ChatLog) -> None:
    skipped = {"not_chat": 0, "before_broadcast": 0}
    for index, entry in enumerate(_items(document, "messages")):
        if not isinstance(entry, dict):
            raise ChatImportError(f"message {index}: expected an object")
        if entry.get("type") != RUSTLOG_PRIVMSG:
            skipped["not_chat"] += 1
            continue
        try:
            sent_at = _timestamp(entry.get("timestamp"))
        except ValueError:
            raise ChatImportError(f"message {index}: timestamp must be ISO 8601 with a UTC offset") from None
        source_ms = round((sent_at - broadcast_start).total_seconds() * 1000)
        if source_ms < 0:
            skipped["before_broadcast"] += 1
            continue
        if source_ms >= 2**31:
            raise ChatImportError(f"message {index}: more than 24 days after the broadcast start")
        tags = _object(entry.get("tags"))
        badges = []
        for badge in str(tags.get("badges") or "").split(",")[:MAX_BADGES]:
            name, _, version = badge.partition("/")
            if name:
                badges.append({"id": name[:50], "version": version[:50]})
        metadata = {"color": _color(tags.get("color")), "badges": badges}
        author = entry.get("displayName") or entry.get("username")
        log.messages.append(_message(index, source_ms, author, entry.get("text"), metadata))
    log.skipped.update(skipped)


def replace_chat(db, video_id: str, log: ChatLog, alignment: Alignment) -> dict:
    """Replace the video's chat in the caller's transaction and return a summary.

    The caller commits. Deleting the old import cascades to its messages, so a
    failure before commit leaves the previous import untouched.
    """
    validate_alignment(alignment)
    video = (
        db.execute(text("SELECT id, duration_seconds FROM videos WHERE id=:id FOR UPDATE"), {"id": video_id})
        .mappings()
        .first()
    )
    if video is None:
        raise ChatImportError("source recording not found")
    end_ms = video["duration_seconds"] * 1000 if video["duration_seconds"] else None
    rows, unmapped, after_end = [], 0, 0
    for message in log.messages:
        offset = alignment.map(message.source_ms)
        if offset is None or offset < 0:
            unmapped += 1
        elif end_ms is not None and offset > end_ms:
            after_end += 1
        else:
            metadata = json.dumps(message.metadata) if message.metadata else None
            rows.append((video_id, offset, message.author, message.body, metadata))
    if not rows:
        raise ChatImportError("no messages fall on the recording's timeline; check the alignment")
    rows.sort(key=lambda row: row[1])
    db.execute(text("DELETE FROM chat_imports WHERE video_id=:id"), {"id": video_id})
    db.execute(
        text("""
            INSERT INTO chat_imports (
                video_id, source_format, source_name, source_digest, broadcast_started_at,
                alignment_method, alignment_mapping, alignment_confidence, message_count
            ) VALUES (
                :video_id, :source_format, :source_name, :digest, :broadcast_started_at,
                :method, CAST(:mapping AS jsonb), :confidence, :count
            )
        """),
        {
            "video_id": video_id,
            "source_format": log.source_format,
            "source_name": log.source_name,
            "digest": log.digest,
            "broadcast_started_at": log.broadcast_started_at,
            "method": alignment.method,
            "mapping": json.dumps([segment.as_dict() for segment in alignment.segments]),
            "confidence": alignment.confidence,
            "count": len(rows),
        },
    )
    cursor = db.connection().connection.driver_connection.cursor()
    with cursor.copy("COPY chat_messages (video_id, offset_ms, author, body, metadata) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
    return {
        "video_id": video_id,
        "source_format": log.source_format,
        "source_digest": log.digest,
        "alignment_method": alignment.method,
        "alignment_confidence": alignment.confidence,
        "alignment_mapping": [segment.as_dict() for segment in alignment.segments],
        "messages_stored": len(rows),
        "dropped": {"outside_alignment": unmapped, "after_recording_end": after_end, **log.skipped},
    }
