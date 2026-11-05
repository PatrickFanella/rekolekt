"""Derive the date a video was broadcast, separately from its YouTube upload date.

VOD channels re-upload streams days, sometimes years, after the broadcast, so
``videos.uploaded_at`` is a poor answer to "when did this happen?". Archive
titles usually carry the broadcast date ("HasanAbi November 25, 2024 – ...",
"HasanAbi Clip [14/09/2025] – ..."), which this module parses.

Each video gets three derived columns:

- ``broadcast_date``: the local calendar date of the broadcast, or NULL.
- ``broadcast_date_source``: ``title``, ``upload_date`` or ``manual``.
  Rows marked ``manual`` are never recomputed.
- ``broadcast_kind``: ``vod`` (a full broadcast), ``no_stream`` (a placeholder
  upload announcing that no broadcast happened that day) or ``extra`` (clips,
  interviews and other material that is not a full broadcast).

Day coverage counts only ``vod`` days as covered and ``no_stream`` days as
confirmed gaps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Literal

from sqlalchemy import text

BroadcastKind = Literal["vod", "no_stream", "extra"]
BroadcastDateSource = Literal["title", "upload_date", "manual"]

_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sept", "sep"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH_ALTERNATION = "|".join(sorted(_MONTHS, key=len, reverse=True))

# "November 25, 2024", "Nov. 25th 2024", "July 17 & 18, 2022" (first day wins)
_LONG_DATE = re.compile(
    rf"(?<![A-Za-z])(?P<month>{_MONTH_ALTERNATION})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s*(?:&|-|–|and)\s*\d{{1,2}}(?:st|nd|rd|th)?)?,?\s+(?P<year>\d{{4}})(?!\d)",
    re.IGNORECASE,
)
# "2025-03-26", "2023/12/31"
_ISO_DATE = re.compile(r"(?<!\d)(?P<year>\d{4})(?P<sep>[-/])(?P<month>\d{1,2})(?P=sep)(?P<day>\d{1,2})(?!\d)")
# "14/09/2025", "26/11/24", "4/07/2021"
_NUMERIC_DATE = re.compile(r"(?<![\d/])(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<year>\d{4}|\d{2})(?![\d/])")

_NO_STREAM = re.compile(r"\bno\s+stream\b", re.IGNORECASE)
_CLIP = re.compile(r"\bclips?\b", re.IGNORECASE)

# Deployment conventions keyed by ARCHIVE_EDITORIAL_PRESET. HasanAbi VOD
# channels write numeric dates day-first and title every full broadcast with
# a spelled-out date, so anything else (clips, TV appearances, podcasts) is an
# extra.
_PRESET_RULES: dict[str, dict[str, object]] = {
    "generic": {"numeric_order": "mdy", "vod_requires_long_date": False},
    "hasanara": {"numeric_order": "dmy", "vod_requires_long_date": True},
}

# A broadcast cannot be dated after its upload. Allow two days of slack for
# time zones and for uploads dated in UTC.
_UPLOAD_SLACK = timedelta(days=2)
_EARLIEST_PLAUSIBLE = date(2005, 1, 1)


@dataclass(frozen=True)
class TitleDate:
    value: date
    style: Literal["long", "iso", "numeric"]


@dataclass(frozen=True)
class BroadcastDate:
    date: date | None
    source: BroadcastDateSource | None
    kind: BroadcastKind


def _rules(preset: str | None) -> dict[str, object]:
    return _PRESET_RULES.get(preset or "generic", _PRESET_RULES["generic"])


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _numeric_date(a: int, b: int, year: int, order: str) -> date | None:
    if year < 100:
        year += 2000
    # An unambiguous day (13-31) settles the order regardless of convention.
    if a > 12 >= b:
        return _safe_date(year, b, a)
    if b > 12 >= a:
        return _safe_date(year, a, b)
    if order == "dmy":
        return _safe_date(year, b, a)
    return _safe_date(year, a, b)


def parse_title_dates(title: str | None, *, preset: str | None = None) -> list[TitleDate]:
    """Return every date written in ``title``, in the order they appear."""

    if not title:
        return []
    order = str(_rules(preset)["numeric_order"])
    found: list[tuple[int, TitleDate]] = []
    for match in _LONG_DATE.finditer(title):
        value = _safe_date(int(match["year"]), _MONTHS[match["month"].lower()], int(match["day"]))
        if value:
            found.append((match.start(), TitleDate(value, "long")))
    for match in _ISO_DATE.finditer(title):
        value = _safe_date(int(match["year"]), int(match["month"]), int(match["day"]))
        if value:
            found.append((match.start(), TitleDate(value, "iso")))
    for match in _NUMERIC_DATE.finditer(title):
        value = _numeric_date(int(match["a"]), int(match["b"]), int(match["year"]), order)
        if value:
            found.append((match.start(), TitleDate(value, "numeric")))
    found.sort(key=lambda item: item[0])
    return [item for _, item in found]


def _upload_day(uploaded_at: date | datetime | None) -> date | None:
    if uploaded_at is None:
        return None
    if isinstance(uploaded_at, datetime):
        if uploaded_at.tzinfo is not None:
            uploaded_at = uploaded_at.astimezone(timezone.utc)
        return uploaded_at.date()
    return uploaded_at


def _plausible(value: date, upload_day: date | None, today: date) -> bool:
    if value < _EARLIEST_PLAUSIBLE or value > today + timedelta(days=1):
        return False
    return upload_day is None or value <= upload_day + _UPLOAD_SLACK


def classify_kind(title: str | None, title_date: TitleDate | None, *, preset: str | None = None) -> BroadcastKind:
    value = title or ""
    if _NO_STREAM.search(value):
        return "no_stream"
    if _CLIP.search(value):
        return "extra"
    if _rules(preset)["vod_requires_long_date"] and (title_date is None or title_date.style != "long"):
        return "extra"
    return "vod"


def derive_broadcast_date(
    title: str | None,
    uploaded_at: date | datetime | None,
    *,
    preset: str | None = None,
    today: date | None = None,
) -> BroadcastDate:
    """Pick the best available broadcast date for one video.

    A plausible title date wins over the upload date; otherwise the upload
    day (UTC) stands in, flagged as ``upload_date`` so callers can tell it is
    an approximation.
    """

    today = today or date.today()
    upload_day = _upload_day(uploaded_at)
    title_date = next(
        (
            candidate
            for candidate in parse_title_dates(title, preset=preset)
            if _plausible(candidate.value, upload_day, today)
        ),
        None,
    )
    kind = classify_kind(title, title_date, preset=preset)
    if title_date is not None:
        return BroadcastDate(title_date.value, "title", kind)
    if upload_day is not None:
        return BroadcastDate(upload_day, "upload_date", kind)
    return BroadcastDate(None, None, kind)


def refresh_broadcast_dates(conn, *, video_ids: Iterable[object] | None = None, preset: str | None = None) -> int:
    """Recompute derived broadcast dates and write the rows that changed.

    Rows whose ``broadcast_date_source`` is ``manual`` keep the editor's value.
    Returns the number of updated videos.
    """

    if preset is None:
        from app.settings import settings

        preset = settings.ARCHIVE_EDITORIAL_PRESET

    params: dict[str, object] = {}
    where = "WHERE broadcast_date_source IS DISTINCT FROM 'manual'"
    if video_ids is not None:
        ids = [str(video_id) for video_id in video_ids]
        if not ids:
            return 0
        where += " AND id = ANY(CAST(:ids AS uuid[]))"
        params["ids"] = ids
    rows = conn.execute(
        text(f"""
            SELECT id, title, uploaded_at, broadcast_date, broadcast_date_source, broadcast_kind
            FROM videos
            {where}
            """),
        params,
    ).mappings()
    today = date.today()
    updates = []
    for row in rows:
        derived = derive_broadcast_date(row["title"], row["uploaded_at"], preset=preset, today=today)
        if (derived.date, derived.source, derived.kind) != (
            row["broadcast_date"],
            row["broadcast_date_source"],
            row["broadcast_kind"],
        ):
            updates.append({"id": row["id"], "date": derived.date, "source": derived.source, "kind": derived.kind})
    if updates:
        conn.execute(
            text("""
                UPDATE videos
                SET broadcast_date = :date,
                    broadcast_date_source = :source,
                    broadcast_kind = :kind
                WHERE id = :id
                  AND broadcast_date_source IS DISTINCT FROM 'manual'
                """),
            updates,
        )
    return len(updates)


def broadcast_columns(title: str | None, uploaded_at: date | datetime | None) -> dict[str, object]:
    """Bind parameters for writing derived broadcast columns with a video row."""

    from app.settings import settings

    derived = derive_broadcast_date(title, uploaded_at, preset=settings.ARCHIVE_EDITORIAL_PRESET)
    return {
        "broadcast_date": derived.date,
        "broadcast_date_source": derived.source,
        "broadcast_kind": derived.kind,
    }


# Midnight UTC of the broadcast date, for code that buckets or orders timestamps.
BROADCAST_AT_SQL = "(v.broadcast_date::timestamp AT TIME ZONE 'UTC')"

MISSING_DATES_LIMIT = 62


@dataclass(frozen=True)
class DayIndex:
    """Which calendar days the archive covers, by broadcast date."""

    first_day: date | None
    vod_days: frozenset[date]
    no_stream_days: frozenset[date]


def load_day_index(conn) -> DayIndex:
    """Read every dated day once; coverage for any period is then computed in memory.

    A day counts as covered only when a full broadcast from it has a
    transcript. "No stream" placeholders count whether or not they were
    transcribed, since they only record an absence.
    """

    from app.archive.repository import ARCHIVE_VIDEO_FILTER_SQL

    rows = conn.execute(text(f"""
            SELECT
                v.broadcast_date AS day,
                bool_or(v.broadcast_kind = 'vod' AND ({ARCHIVE_VIDEO_FILTER_SQL})) AS has_vod,
                bool_or(v.broadcast_kind = 'no_stream') AS has_no_stream
            FROM videos v
            WHERE v.broadcast_date IS NOT NULL
              AND v.broadcast_kind IN ('vod', 'no_stream')
            GROUP BY v.broadcast_date
            """)).mappings().all()
    vod_days: set[date] = set()
    no_stream_days: set[date] = set()
    for row in rows:
        if row["has_vod"]:
            vod_days.add(row["day"])
        elif row["has_no_stream"]:
            no_stream_days.add(row["day"])
    known = vod_days | no_stream_days
    return DayIndex(min(known) if known else None, frozenset(vod_days), frozenset(no_stream_days))


def coverage_for_range(index: DayIndex, date_from: date, date_to: date, *, today: date | None = None) -> dict | None:
    """Coverage of the inclusive range, clipped to the archive's span and today.

    Returns None when the range lies entirely outside the archive's span, so
    callers do not present a pre-archive or future period as 0 % covered.
    """

    if index.first_day is None:
        return None
    lower = max(date_from, index.first_day)
    upper = min(date_to, today or date.today())
    if upper < lower:
        return None
    days = (upper - lower).days + 1
    vod_days = sum(1 for day in index.vod_days if lower <= day <= upper)
    no_stream_days = sum(1 for day in index.no_stream_days if lower <= day <= upper)
    missing: list[date] = []
    day = lower
    while day <= upper and len(missing) < MISSING_DATES_LIMIT:
        if day not in index.vod_days and day not in index.no_stream_days:
            missing.append(day)
        day += timedelta(days=1)
    return {
        "days": days,
        "vod_days": vod_days,
        "no_stream_days": no_stream_days,
        "unknown_days": days - vod_days - no_stream_days,
        "missing_dates": missing,
    }


def video_broadcast_at(video) -> datetime | None:
    """Midnight UTC of a VideoInfo's broadcast date, falling back to its ingestion time."""

    value = getattr(video, "broadcast_date", None)
    if value is not None:
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    return getattr(video, "created_at", None)
