"""Reviewed transcript corrections applied over machine transcripts.

An accepted correction is written into the segment row itself, so search (through
the outbox triggers), exports, passage pages and clip handoffs read the corrected
text without knowing about corrections. The machine text and its digest stay on
the correction row. Re-transcription rewrites segment rows; ``reapply_corrections``
then re-applies each accepted correction whose segment still carries the same
machine text at the same time range, and marks the others stale for review.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import text

from app import crud
from app.transcripts.blocks import build_transcript_blocks
from app.transcripts.types import TranscriptSegment

SOURCES = ("whisper", "youtube")

# (UPDATE target, extra FROM items, join of segment row to video) per source.
_SEGMENT_TABLES = {
    "whisper": ("segments s", "", "s.video_id = :v"),
    "youtube": (
        "youtube_segments s",
        "youtube_transcripts t",
        "t.id = s.youtube_transcript_id AND t.video_id = :v",
    ),
}
_KEY = "s.start_ms = :start AND s.end_ms = :end"
_SQL_DIGEST = "encode(sha256(convert_to(s.text, 'UTF8')), 'hex')"


class CorrectionConflictError(Exception):
    """The proposal no longer matches the machine text it was written against."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _update(source: str, *extra: str) -> tuple[str, str]:
    """UPDATE target and its trailing ``[FROM ...] WHERE ...`` for one source's segments."""
    table, joined, where = _SEGMENT_TABLES[source]
    sources = ", ".join(item for item in (joined, *extra) if item)
    return table, (f"FROM {sources} " if sources else "") + f"WHERE {where}"


def _select(source: str) -> str:
    """``FROM ... WHERE ...`` selecting one video's segments as ``s``."""
    table, joined, where = _SEGMENT_TABLES[source]
    return f"FROM {table}{', ' + joined if joined else ''} WHERE {where}"


def segment_text(db, video_id, source: str, start_ms: int, end_ms: int, *, lock: bool = False) -> str | None:
    """Return the text the segment at this range currently shows, or None."""
    row = db.execute(
        text(f"SELECT s.text {_select(source)} AND {_KEY} ORDER BY s.id LIMIT 1 {'FOR UPDATE OF s' if lock else ''}"),
        {"v": str(video_id), "start": start_ms, "end": end_ms},
    ).first()
    return None if row is None else row[0]


def machine_text(db, video_id, source: str, start_ms: int, end_ms: int, current: str) -> str:
    """Undo the applied correction, if the segment currently shows one."""
    applied = (
        db.execute(
            text("""
                SELECT original_text, corrected_text FROM segment_corrections
                WHERE video_id=:v AND source=:src AND start_ms=:start AND end_ms=:end AND status='accepted'
            """),
            {"v": str(video_id), "src": source, "start": start_ms, "end": end_ms},
        )
        .mappings()
        .first()
    )
    if applied and applied["corrected_text"] == current:
        return str(applied["original_text"])
    return current


def _set_segment_text(db, video_id, source: str, start_ms: int, end_ms: int, value: str) -> None:
    table, where = _update(source)
    db.execute(
        text(f"UPDATE {table} SET text=:value {where} AND {_KEY} AND s.text <> :value"),
        {"v": str(video_id), "start": start_ms, "end": end_ms, "value": value},
    )


def refresh_derived_text(db, video_id, source: str) -> None:
    """Rewrite stored full text (and native formatted blocks) from current segments."""
    params = {"v": str(video_id), "src": source}
    has_corrections = (
        "EXISTS (SELECT 1 FROM segment_corrections c WHERE c.video_id=:v AND c.source=:src"
        " AND c.accepted_at IS NOT NULL)"
    )
    if source == "youtube":
        db.execute(
            text(f"""
                UPDATE youtube_transcripts t SET full_text = (
                    SELECT string_agg(s.text, ' ' ORDER BY s.start_ms, s.id)
                    FROM youtube_segments s WHERE s.youtube_transcript_id = t.id)
                WHERE t.video_id=:v AND {has_corrections}
            """),
            params,
        )
        return
    if not db.execute(text(f"SELECT {has_corrections}"), params).scalar():
        return
    rows = db.execute(
        text("SELECT start_ms, end_ms, text, speaker_label FROM segments WHERE video_id=:v ORDER BY start_ms, id"),
        params,
    ).all()
    db.execute(
        text("UPDATE transcripts SET full_text=:full WHERE video_id=:v"),
        {"v": str(video_id), "full": " ".join(row[2] for row in rows)},
    )
    crud.replace_transcript_blocks(db, video_id, build_transcript_blocks([TranscriptSegment(*row) for row in rows]))


def reapply_corrections(db, video_id, source: str) -> None:
    """Re-apply accepted corrections after a source rewrite, in the caller's transaction.

    A correction applies only where a segment at the same range still has the
    machine text it was accepted against. Every other accepted correction becomes
    ``stale`` and stays listed for moderators; nothing is dropped or applied to
    different text.
    """
    table, where = _update(source, "segment_corrections c")
    match = "c.video_id=:v AND c.source=:src AND c.accepted_at IS NOT NULL AND c.status IN ('accepted','stale')"
    params = {"v": str(video_id), "src": source}
    db.execute(
        text(f"""
            UPDATE {table} SET text = c.corrected_text {where} AND {match}
              AND s.start_ms = c.start_ms AND s.end_ms = c.end_ms AND {_SQL_DIGEST} = c.original_sha256
        """),
        params,
    )
    db.execute(
        text(f"""
            UPDATE segment_corrections c SET updated_at=now(), status = CASE WHEN EXISTS (
                SELECT 1 {_select(source)}
                  AND s.start_ms = c.start_ms AND s.end_ms = c.end_ms AND s.text = c.corrected_text
            ) THEN 'accepted' ELSE 'stale' END
            WHERE {match}
        """),
        params,
    )
    refresh_derived_text(db, video_id, source)


def accept_correction(db, correction: dict, *, reviewer_id, reason: str) -> None:
    """Apply a locked pending correction, superseding any earlier one at the same range.

    Raises CorrectionConflictError (after marking the proposal stale) when the segment
    no longer carries the machine text the proposal was written against.
    """
    key = (correction["video_id"], correction["source"], correction["start_ms"], correction["end_ms"])
    current = segment_text(db, *key, lock=True)
    machine = None if current is None else machine_text(db, *key, current)
    if machine is None or digest(machine) != correction["original_sha256"]:
        db.execute(
            text("UPDATE segment_corrections SET status='stale', updated_at=now() WHERE id=:id"),
            {"id": correction["id"]},
        )
        raise CorrectionConflictError
    _set_segment_text(db, *key, correction["corrected_text"])
    db.execute(
        text("""
            UPDATE segment_corrections SET status='superseded', updated_at=now()
            WHERE video_id=:v AND source=:src AND start_ms=:start AND end_ms=:end
              AND accepted_at IS NOT NULL AND status IN ('accepted','stale') AND id <> :id
        """),
        {"v": str(key[0]), "src": key[1], "start": key[2], "end": key[3], "id": correction["id"]},
    )
    db.execute(
        text("""
            UPDATE segment_corrections
            SET status='accepted', accepted_at=now(), reviewed_at=now(), reviewer_id=:reviewer,
                review_reason=:reason, updated_at=now()
            WHERE id=:id
        """),
        {"id": correction["id"], "reviewer": reviewer_id, "reason": reason},
    )
    refresh_derived_text(db, key[0], key[1])


def applied_marks(db, video_id, source: str) -> list[dict]:
    """Ranges currently showing an accepted correction, with their machine text."""
    rows = db.execute(
        text("""
            SELECT start_ms, end_ms, original_text AS machine_text FROM segment_corrections
            WHERE video_id=:v AND source=:src AND status='accepted' ORDER BY start_ms, end_ms
        """),
        {"v": str(video_id), "src": source},
    ).mappings()
    return [dict(row) for row in rows]
