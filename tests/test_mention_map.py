"""Mention maps count every matched moment in one pass against real search SQL."""

import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app import crud
from app.search.segment_repository import SearchRepository

TODAY = datetime.now(timezone.utc).date()


def _video(db, *, title, broadcast_date=None, created_at=None, language="en", youtube_id=None):
    video_id = uuid.uuid4()
    job_id = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO jobs (id, kind, input_url, state) VALUES (:id, 'single', 'https://example.test', 'completed')"
        ),
        {"id": str(job_id)},
    )
    created = created_at or datetime(2024, 1, 1, tzinfo=timezone.utc)
    db.execute(
        text("""
            INSERT INTO videos (id, job_id, youtube_id, idx, title, state, uploaded_at, created_at, updated_at,
                                broadcast_date, language, channel_name)
            VALUES (:id, :job_id, :youtube_id, 0, :title, 'completed', :created, :created, :created,
                    :broadcast_date, :language, 'HasanAbi')
            """),
        {
            "id": str(video_id),
            "job_id": str(job_id),
            "youtube_id": youtube_id or video_id.hex[:11],
            "title": title,
            "created": created,
            "broadcast_date": broadcast_date,
            "language": language,
        },
    )
    return str(video_id)


def _native(db, video_id, *lines):
    for index, line in enumerate(lines):
        db.execute(
            text("INSERT INTO segments (video_id, start_ms, end_ms, text) VALUES (:vid, :start, :end, :text)"),
            {"vid": video_id, "start": index * 1000, "end": index * 1000 + 900, "text": line},
        )


def _captions(db, video_id, *lines):
    transcript_id = uuid.uuid4()
    db.execute(
        text("INSERT INTO youtube_transcripts (id, video_id) VALUES (:id, :vid)"),
        {"id": str(transcript_id), "vid": video_id},
    )
    for index, line in enumerate(lines):
        db.execute(
            text("""
                INSERT INTO youtube_segments (youtube_transcript_id, start_ms, end_ms, text)
                VALUES (:tid, :start, :end, :text)
                """),
            {"tid": str(transcript_id), "start": index * 1000, "end": index * 1000 + 900, "text": line},
        )


@pytest.fixture
def archive(db_session):
    db = db_session
    ids = {}
    ids["old"] = _video(db, title="Old stream", broadcast_date=date(2021, 3, 4))
    _native(db, ids["old"], "intro", "landlords and housing costs", "nothing here", "housing again later")
    ids["titled"] = _video(db, title="Housing crisis deep dive", broadcast_date=TODAY - timedelta(days=10))
    _native(db, ids["titled"], "hello chat", "rent control and housing", "landlords dread rent control housing")
    ids["title_only"] = _video(db, title="Housing night", broadcast_date=date(2022, 5, 6))
    _native(db, ids["title_only"], "nothing relevant", "still nothing")
    ids["captions"] = _video(db, title="Caption stream", broadcast_date=date(2023, 7, 8), language="fr")
    _captions(db, ids["captions"], "caption housing line", "other caption", "rent and housing landlords")
    # Native transcripts win: this video's captions must never be counted.
    ids["shadowed"] = _video(db, title="Shadowed", broadcast_date=date(2023, 1, 1))
    _native(db, ids["shadowed"], "unrelated")
    _captions(db, ids["shadowed"], "housing in captions only")
    # Undated broadcasts fall back to the ingestion time.
    ids["undated"] = _video(db, title="Untitled", created_at=datetime(2020, 2, 2, tzinfo=timezone.utc))
    _native(db, ids["undated"], "housing first ever")
    return ids


SEARCHES = {"best": "search_best", "native": "search_native", "youtube": "search_youtube"}


@pytest.mark.parametrize("source", sorted(SEARCHES))
@pytest.mark.parametrize(
    "filters", [{}, {"language": "en"}, {"date_from": "2022-01-01"}, {"match_mode": "whole_word"}], ids=str
)
def test_counts_match_paging_through_every_search_result(db_session, archive, source, filters):
    repo = SearchRepository()
    rows = getattr(repo, SEARCHES[source])(db_session, q="housing", limit=10_000, filters=dict(filters))
    expected = Counter(str(row["video_id"]) for row in rows)

    counts = repo.mention_counts(db_session, "housing", source, None, dict(filters))

    assert {str(row["video_id"]): row["moments"] for row in counts} == dict(expected)


def test_mention_map_counts_complete_matches_by_broadcast_date(db_session, archive):
    result = crud.get_mention_map(db_session, q="housing")

    # old 2 spoken; titled 2 spoken + 1 title; title_only 1 title; captions 2
    # spoken; undated 1 spoken. Shadowed captions are excluded.
    assert result.total_moments == 9
    assert result.total_videos == 5
    assert result.recent_mentions_90d == 3
    assert result.first_mentioned_year == 2020
    assert result.first_mention.video_id == uuid.UUID(archive["undated"])
    assert result.latest_mention.video_id == uuid.UUID(archive["titled"])
    assert result.latest_mention.start_ms == 2000
    assert result.latest_mention.highlights
    top = result.top_episodes[0]
    assert top.video.id == uuid.UUID(archive["titled"])
    assert len(top.moments) == 3
    assert {moment.broadcast_date for moment in top.moments} == {TODAY - timedelta(days=10)}
    assert "landlords" in result.related_topics


def test_mention_map_cites_title_matches_only_when_nothing_was_spoken(db_session, archive):
    result = crud.get_mention_map(db_session, q="night")

    assert result.total_moments == 1
    assert result.first_mention.video_id == uuid.UUID(archive["title_only"])
    assert result.first_mention.title_match
    assert result.related_topics == []


def test_mention_map_respects_filters(db_session, archive):
    result = crud.get_mention_map(db_session, q="housing", filters={"language": "fr"})

    assert result.total_videos == 1
    assert result.total_moments == 2
    assert result.first_mention.video_id == uuid.UUID(archive["captions"])
    assert result.first_mention.source == "youtube"


def test_mention_map_without_matches_is_empty(db_session, archive):
    result = crud.get_mention_map(db_session, q="zeppelin")

    assert result.total_moments == 0
    assert result.first_mention is None
    assert result.top_episodes == []
