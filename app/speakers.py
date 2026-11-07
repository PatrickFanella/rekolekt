"""Attribute diarized speaker labels to archive people.

A recording's diarized labels (``Speaker 1``...) are anonymous and local to that
recording. ``video_speaker_people`` maps a label to an archive person, either by
an administrator (``manual``) or by comparing the label's stored pyannote
embedding with enrolled voices (``auto``). Enrolled voices are embeddings copied
from labels an administrator confirmed.
"""

from __future__ import annotations

import math
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import text

from app.exceptions import NotFoundError, ValidationError

MAX_LABELS = 100
SAMPLES_PER_LABEL = 3
MAX_REFERENCE_SEGMENTS = 50
MAX_REFERENCES = 5000


def cosine(a: Iterable[float], b: Iterable[float]) -> float:
    a, b = list(a), list(b)
    if len(a) != len(b) or not a:
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else -1.0


def _person(db, slug: str) -> dict:
    row = (
        db.execute(text("SELECT id, slug, display_name, status FROM archive_people WHERE slug = :slug"), {"slug": slug})
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError(f"Archive person {slug} not found", resource_type="archive_person")
    return dict(row)


def _require_label(db, video_id: UUID, speaker_label: str) -> None:
    exists = db.execute(
        text("SELECT 1 FROM segments WHERE video_id = :v AND speaker_label = :label LIMIT 1"),
        {"v": str(video_id), "label": speaker_label},
    ).first()
    if exists is None:
        raise NotFoundError(f"Speaker label {speaker_label} not found for this video", resource_type="speaker_label")


def _centroids(db, video_id: UUID) -> dict[str, dict]:
    rows = db.execute(
        text("""
            SELECT speaker_label, embedding, model, speech_seconds
            FROM video_speaker_centroids WHERE video_id = :v
        """),
        {"v": str(video_id)},
    ).mappings()
    return {row["speaker_label"]: dict(row) for row in rows}


def _references(db, models: set[str], exclude_video_id: UUID | None = None) -> list[dict]:
    if not models:
        return []
    rows = db.execute(
        text("""
            SELECT r.person_id, r.video_id, r.embedding, r.model, p.slug, p.display_name
            FROM speaker_voice_references r JOIN archive_people p ON p.id = r.person_id
            WHERE r.model = ANY(:models)
              AND (CAST(:exclude AS uuid) IS NULL OR r.video_id <> CAST(:exclude AS uuid))
            ORDER BY r.created_at, r.id
            LIMIT :limit
        """),
        {
            "models": sorted(models),
            "exclude": str(exclude_video_id) if exclude_video_id else None,
            "limit": MAX_REFERENCES,
        },
    ).mappings()
    return [dict(row) for row in rows]


def best_person(centroid: dict, references: list[dict]) -> dict | None:
    """Return the enrolled person whose closest reference voice is most similar."""
    best: dict | None = None
    for reference in references:
        if reference["model"] != centroid["model"]:
            continue
        score = cosine(centroid["embedding"], reference["embedding"])
        if best is None or score > best["score"]:
            best = {
                "person_id": reference["person_id"],
                "slug": reference["slug"],
                "display_name": reference["display_name"],
                "score": score,
            }
    return best


def suggest_people(db, video_id: UUID) -> dict[str, dict]:
    """Best enrolled match per label, ignoring references taken from this recording."""
    centroids = _centroids(db, video_id)
    references = _references(db, {c["model"] for c in centroids.values()}, exclude_video_id=video_id)
    suggestions = {}
    for label, centroid in centroids.items():
        match = best_person(centroid, references)
        if match is not None:
            suggestions[label] = match
    return suggestions


def list_video_speakers(db, video_id: UUID) -> list[dict]:
    """Distinct labels with segment counts, sample segments, mapping and suggestion."""
    labels = db.execute(
        text("""
            SELECT s.speaker_label, count(*) AS segment_count, c.speech_seconds,
                   m.method, m.score, m.set_by, m.set_at, m.invalidated_at, m.invalidated_reason,
                   p.slug AS person_slug, p.display_name AS person_display_name
            FROM segments s
            LEFT JOIN video_speaker_centroids c ON c.video_id = s.video_id AND c.speaker_label = s.speaker_label
            LEFT JOIN video_speaker_people m ON m.video_id = s.video_id AND m.speaker_label = s.speaker_label
            LEFT JOIN archive_people p ON p.id = m.person_id
            WHERE s.video_id = :v AND s.speaker_label IS NOT NULL
            GROUP BY s.speaker_label, c.speech_seconds, m.method, m.score, m.set_by, m.set_at,
                     m.invalidated_at, m.invalidated_reason, p.slug, p.display_name
            ORDER BY min(s.start_ms), s.speaker_label
            LIMIT :limit
        """),
        {"v": str(video_id), "limit": MAX_LABELS},
    ).mappings()
    samples: dict[str, list[dict]] = {}
    for row in db.execute(
        text("""
            SELECT speaker_label, id, start_ms, end_ms, text FROM (
                SELECT speaker_label, id, start_ms, end_ms, text,
                       row_number() OVER (PARTITION BY speaker_label ORDER BY end_ms - start_ms DESC, id) AS rank
                FROM segments WHERE video_id = :v AND speaker_label IS NOT NULL
            ) ranked
            WHERE rank <= :samples
            ORDER BY speaker_label, start_ms
        """),
        {"v": str(video_id), "samples": SAMPLES_PER_LABEL},
    ).mappings():
        samples.setdefault(row["speaker_label"], []).append(
            {"id": row["id"], "start_ms": row["start_ms"], "end_ms": row["end_ms"], "text": row["text"]}
        )
    suggestions = suggest_people(db, video_id)
    items = []
    for row in labels:
        mapping = None
        if row["method"] is not None:
            if row["invalidated_at"] is not None:
                status = "stale"
            elif row["person_slug"] is None:
                status = "cleared"
            else:
                status = "active"
            mapping = {
                "person_slug": row["person_slug"],
                "person_display_name": row["person_display_name"],
                "method": row["method"],
                "score": row["score"],
                "set_by": row["set_by"],
                "set_at": row["set_at"],
                "status": status,
                "invalidated_at": row["invalidated_at"],
                "invalidated_reason": row["invalidated_reason"],
            }
        suggestion = suggestions.get(row["speaker_label"])
        items.append(
            {
                "speaker_label": row["speaker_label"],
                "segment_count": row["segment_count"],
                "speech_seconds": row["speech_seconds"],
                "has_embedding": row["speech_seconds"] is not None,
                "samples": samples.get(row["speaker_label"], []),
                "mapping": mapping,
                "suggestion": (
                    {k: suggestion[k] for k in ("slug", "display_name", "score")} if suggestion is not None else None
                ),
            }
        )
    return items


def _touch_and_reindex(db, video_id: UUID, speaker_label: str) -> int:
    """Queue the label's segments for reindexing and expire cached search results."""
    queued = db.execute(
        text("""
            INSERT INTO search_index_outbox(source, document_id, video_id, operation, version, payload)
            SELECT 'native', s.id, s.video_id, 'upsert',
                   (extract(epoch FROM clock_timestamp()) * 1000000)::bigint, native_search_payload(s)
            FROM segments s WHERE s.video_id = :v AND s.speaker_label = :label
        """),
        {"v": str(video_id), "label": speaker_label},
    ).rowcount
    # Search result cache keys include the newest video edit.
    db.execute(text("UPDATE videos SET updated_at = clock_timestamp() WHERE id = :v"), {"v": str(video_id)})
    return int(queued or 0)


def _upsert_mapping(
    db,
    video_id: UUID,
    speaker_label: str,
    person_id: Any,
    *,
    method: str,
    score: float | None,
    set_by: Any,
) -> None:
    db.execute(
        text("""
            INSERT INTO video_speaker_people (video_id, speaker_label, person_id, method, score, set_by, set_at)
            VALUES (:v, :label, :person_id, :method, :score, :set_by, now())
            ON CONFLICT (video_id, speaker_label) DO UPDATE SET
                person_id = EXCLUDED.person_id, method = EXCLUDED.method, score = EXCLUDED.score,
                set_by = EXCLUDED.set_by, set_at = EXCLUDED.set_at,
                invalidated_at = NULL, invalidated_reason = NULL
        """),
        {
            "v": str(video_id),
            "label": speaker_label,
            "person_id": str(person_id) if person_id else None,
            "method": method,
            "score": score,
            "set_by": str(set_by) if set_by else None,
        },
    )


def set_speaker_person(db, video_id: UUID, speaker_label: str, person_slug: str, *, user_id: Any) -> dict:
    """Map a label to a person by hand; returns the person and queued document count."""
    _require_label(db, video_id, speaker_label)
    person = _person(db, person_slug)
    _upsert_mapping(db, video_id, speaker_label, person["id"], method="manual", score=None, set_by=user_id)
    return {"person": person, "reindexed": _touch_and_reindex(db, video_id, speaker_label)}


def clear_speaker_person(db, video_id: UUID, speaker_label: str, *, user_id: Any) -> int:
    """Record that the label belongs to no known person, so matching leaves it alone."""
    _require_label(db, video_id, speaker_label)
    _upsert_mapping(db, video_id, speaker_label, None, method="manual", score=None, set_by=user_id)
    return _touch_and_reindex(db, video_id, speaker_label)


def add_voice_reference(
    db, person_slug: str, video_id: UUID, speaker_label: str, segment_ids: list[int], *, user_id: Any
) -> dict:
    """Enroll a confirmed voice: the label's stored embedding becomes a reference for the person.

    The marked segments must all belong to the recording and carry the label;
    they are kept as the evidence an administrator confirmed.
    """
    person = _person(db, person_slug)
    ids = sorted(set(segment_ids))
    if not ids or len(ids) > MAX_REFERENCE_SEGMENTS:
        raise ValidationError(f"Mark between 1 and {MAX_REFERENCE_SEGMENTS} segments", field="segment_ids")
    matched = db.execute(
        text("""
            SELECT count(*) FROM segments
            WHERE id = ANY(:ids) AND video_id = :v AND speaker_label = :label
        """),
        {"ids": ids, "v": str(video_id), "label": speaker_label},
    ).scalar_one()
    if matched != len(ids):
        raise ValidationError(
            "Every marked segment must belong to this recording and speaker label", field="segment_ids"
        )
    centroid = _centroids(db, video_id).get(speaker_label)
    if centroid is None:
        raise ValidationError(
            "This speaker label has no stored voice embedding; diarize the recording again first",
            field="speaker_label",
        )
    row = (
        db.execute(
            text("""
            INSERT INTO speaker_voice_references
                (person_id, video_id, speaker_label, segment_ids, embedding, model, created_by)
            VALUES (:person_id, :v, :label, :ids, :embedding, :model, :user_id)
            ON CONFLICT (person_id, video_id, speaker_label) DO UPDATE SET
                segment_ids = EXCLUDED.segment_ids, embedding = EXCLUDED.embedding, model = EXCLUDED.model,
                created_by = EXCLUDED.created_by, created_at = now()
            RETURNING id, video_id, speaker_label, segment_ids, model, created_by, created_at
        """),
            {
                "person_id": str(person["id"]),
                "v": str(video_id),
                "label": speaker_label,
                "ids": ids,
                "embedding": list(centroid["embedding"]),
                "model": centroid["model"],
                "user_id": str(user_id) if user_id else None,
            },
        )
        .mappings()
        .one()
    )
    return dict(row)


def list_voice_references(db, person_slug: str) -> list[dict]:
    person = _person(db, person_slug)
    rows = db.execute(
        text("""
            SELECT id, video_id, speaker_label, segment_ids, model, created_by, created_at
            FROM speaker_voice_references WHERE person_id = :p
            ORDER BY created_at DESC, id LIMIT 500
        """),
        {"p": str(person["id"])},
    ).mappings()
    return [dict(row) for row in rows]


def delete_voice_reference(db, person_slug: str, reference_id: UUID) -> None:
    person = _person(db, person_slug)
    deleted = db.execute(
        text("DELETE FROM speaker_voice_references WHERE id = :id AND person_id = :p"),
        {"id": str(reference_id), "p": str(person["id"])},
    ).rowcount
    if not deleted:
        raise NotFoundError("Voice reference not found", resource_type="speaker_voice_reference")


def auto_match_video(db, video_id: UUID, threshold: float) -> list[dict]:
    """Write ``auto`` mappings for labels whose best enrolled match reaches the threshold.

    Labels that already have a current mapping (including one an administrator
    cleared) are left alone; stale mappings may be replaced.
    """
    current = {
        row[0]
        for row in db.execute(
            text("SELECT speaker_label FROM video_speaker_people WHERE video_id = :v AND invalidated_at IS NULL"),
            {"v": str(video_id)},
        )
    }
    written = []
    for label, match in suggest_people(db, video_id).items():
        if label in current or match["score"] < threshold:
            continue
        _upsert_mapping(db, video_id, label, match["person_id"], method="auto", score=match["score"], set_by=None)
        _touch_and_reindex(db, video_id, label)
        written.append({"speaker_label": label, "slug": match["slug"], "score": match["score"]})
    return written


def transcript_speaker_people(db, video_id: UUID) -> dict[str, dict]:
    """Current label-to-person mappings for published people, for transcript responses."""
    rows = db.execute(
        text("""
            SELECT m.speaker_label, p.slug, p.display_name
            FROM video_speaker_people m JOIN archive_people p ON p.id = m.person_id
            WHERE m.video_id = :v AND m.invalidated_at IS NULL AND p.status = 'published'
        """),
        {"v": str(video_id)},
    ).mappings()
    return {row["speaker_label"]: {"slug": row["slug"], "display_name": row["display_name"]} for row in rows}


def evaluate_thresholds(labeled: list[dict], predictions: list[dict], thresholds: list[float]) -> list[dict]:
    """Precision and recall of automatic matching at each threshold.

    ``labeled`` rows carry ``video_id``, ``speaker_label`` and the true
    ``person_slug`` (empty for a speaker who is not an enrolled person).
    ``predictions`` carry the matcher's best ``slug`` and ``score`` per label.
    A prediction counts when its score reaches the threshold; it is correct
    when its slug equals the true slug. Recall is over labels whose true person
    is known.
    """
    predicted = {(str(p["video_id"]), p["speaker_label"]): p for p in predictions}
    known = [row for row in labeled if row["person_slug"]]
    results = []
    for threshold in thresholds:
        made = correct = 0
        for row in labeled:
            prediction = predicted.get((str(row["video_id"]), row["speaker_label"]))
            if prediction is None or prediction["score"] < threshold:
                continue
            made += 1
            correct += int(prediction["slug"] == row["person_slug"])
        results.append(
            {
                "threshold": threshold,
                "predicted": made,
                "correct": correct,
                "precision": correct / made if made else None,
                "recall": correct / len(known) if known else None,
            }
        )
    return results
