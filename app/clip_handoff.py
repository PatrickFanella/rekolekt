"""Versioned, token-free provenance handoff for a completed passage clip.

The transcript snapshot is taken when the export is requested and stored with the
private job record, so a later transcript edit or re-ingest never relabels the
original selection. The handoff declares provenance; it is not rights proof.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from sqlalchemy import text

SCHEMA = "rekolekt.clip-handoff"
SCHEMA_VERSION = 1
MAX_CUES = 200
MAX_CUE_CHARS = 2000
TRANSCRIPT_SOURCES = ("whisper", "youtube")
PROVENANCE_STATEMENT = (
    "Declared archive provenance. It records which recording, range and transcript were selected; "
    "it is not independent proof of rights to use the media."
)


class TranscriptUnavailableError(LookupError):
    pass


def _line(start_ms: int, end_ms: int | None, text_value: str) -> bytes:
    return (json.dumps([start_ms, end_ms, text_value], ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _whisper(db, video_id: str):
    params = {"v": video_id}
    transcript = (
        db.execute(
            text("SELECT id, language FROM transcripts WHERE video_id=:v ORDER BY created_at DESC, id LIMIT 1"),
            params,
        )
        .mappings()
        .first()
    )
    rows = db.execute(
        text("SELECT start_ms, end_ms, text FROM segments WHERE video_id=:v ORDER BY start_ms, id"), params
    ).all()
    if not rows:
        rows = db.execute(
            text("""
                SELECT s.start_ms, s.end_ms, s.text FROM segments s
                JOIN transcripts t ON t.id = s.transcript_id
                WHERE t.video_id=:v ORDER BY s.start_ms, s.id
            """),
            params,
        ).all()
    identity = {
        "source": "whisper",
        "transcript_id": str(transcript["id"]) if transcript else None,
        "language": transcript["language"] if transcript else None,
        "kind": "machine",
    }
    return identity, rows


def _youtube(db, video_id: str):
    transcript = (
        db.execute(
            text("SELECT id, language, kind FROM youtube_transcripts WHERE video_id=:v"),
            {"v": video_id},
        )
        .mappings()
        .first()
    )
    if not transcript:
        return None, []
    rows = db.execute(
        text(
            "SELECT start_ms, end_ms, text FROM youtube_segments WHERE youtube_transcript_id=:t ORDER BY start_ms, id"
        ),
        {"t": str(transcript["id"])},
    ).all()
    identity = {
        "source": "youtube",
        "transcript_id": str(transcript["id"]),
        "language": transcript["language"],
        "kind": f"caption-{transcript['kind'] or 'auto'}",
    }
    return identity, rows


def snapshot_transcript(db, video_id: str, start_ms: int, end_ms: int, source: str | None = None) -> dict[str, Any]:
    """Capture the selected transcript's identity, revision digest and clip-relative cues.

    With no explicit source, use the native (Whisper) transcript when present, then
    captions, matching the public passage page. An explicit source that has no
    segments raises TranscriptUnavailableError; an absent default is recorded as such.
    """
    if source is not None and source not in TRANSCRIPT_SOURCES:
        raise ValueError("Unsupported transcript source")
    # Read directly rather than through cached helpers so deleted text never reappears.
    candidates = [source] if source else list(TRANSCRIPT_SOURCES)
    identity, rows = None, []
    for candidate in candidates:
        identity, rows = (_whisper if candidate == "whisper" else _youtube)(db, video_id)
        if rows:
            break
    captured_at = time.time()
    if not rows:
        if source:
            raise TranscriptUnavailableError(f"The selected {source} transcript is not available for this recording")
        return {
            "captured_at": captured_at,
            "transcript": {"status": "unavailable"},
            "cues": [],
            "cues_truncated": False,
            "cue_text_sha256": None,
        }

    revision = hashlib.sha256()
    cues: list[dict[str, Any]] = []
    truncated = False
    for row_start, row_end, row_text in rows:
        row_text = row_text or ""
        revision.update(_line(row_start, row_end, row_text))
        timed = row_end is not None and row_end > row_start
        overlaps = row_start < end_ms and (row_end > start_ms if timed else row_start >= start_ms)
        if not overlaps:
            continue
        if len(cues) >= MAX_CUES:
            truncated = True
            continue
        cue: dict[str, Any] = {
            "start_ms": max(row_start, start_ms) - start_ms,
            # Untimed or zero-length segments keep their start; the end is not invented.
            "end_ms": min(row_end, end_ms) - start_ms if timed else None,
            "text": row_text[:MAX_CUE_CHARS],
        }
        if len(row_text) > MAX_CUE_CHARS:
            cue["text_truncated"] = True
        cues.append(cue)
    cue_digest = hashlib.sha256(b"".join(_line(c["start_ms"], c["end_ms"], c["text"]) for c in cues))
    return {
        "captured_at": captured_at,
        "transcript": {
            "status": "captured",
            **(identity or {}),
            "segment_count": len(rows),
            "revision_sha256": revision.hexdigest(),
            "timing": "segment",
        },
        "cues": cues,
        "cues_truncated": truncated,
        "cue_text_sha256": cue_digest.hexdigest(),
    }


def provenance_key(provenance: dict | None) -> tuple:
    """Identity used to deduplicate exports: a changed transcript is a new selection."""
    transcript = (provenance or {}).get("transcript") or {}
    return (transcript.get("source"), transcript.get("revision_sha256"))


def build_handoff(job: dict, *, passage_url: str, player_url: str, origin: str, site_name: str) -> dict[str, Any]:
    provenance = job["provenance"]
    youtube = job.get("source_kind") == "youtube"
    media_source: dict[str, Any] = {"kind": "youtube-range" if youtube else "registered-original"}
    if youtube:
        media_source["youtube_id"] = job["youtube_id"]
    else:
        media_source["original_sha256"] = job["source_sha256"]
        media_source["timeline_aligned"] = True
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "export_id": job["id"],
        "generated_at": time.time(),
        "archive": {"name": site_name, "origin": origin},
        "recording": {"video_id": job["video_id"], "youtube_id": provenance.get("youtube_id")},
        "passage": {
            "url": passage_url,
            "player_url": player_url,
            "start_ms": job["start_ms"],
            "end_ms": job["end_ms"],
            "duration_ms": job["end_ms"] - job["start_ms"],
        },
        "transcript": {**provenance["transcript"], "captured_at": provenance["captured_at"]},
        "captions": {
            "timing": "clip-relative" if provenance["cues"] else "unavailable",
            "cues": provenance["cues"],
            "cues_truncated": provenance["cues_truncated"],
            "text_sha256": provenance["cue_text_sha256"],
        },
        "media": {
            "type": "video/mp4",
            "filename": f"passage-{job['id']}.mp4",
            "bytes": job["output_bytes"],
            "sha256": job["output_sha256"],
        },
        "provenance": {"basis": "declared", "statement": PROVENANCE_STATEMENT, "media_source": media_source},
    }
