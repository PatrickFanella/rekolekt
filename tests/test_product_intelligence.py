from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from app.archive.product_intelligence import build_topic_timeline, quoted_moments, related_episodes
from app.schemas import SearchMoment, VideoInfo


def test_topic_timeline_counts_mentions_episodes_and_cited_evidence(monkeypatch):
    video = VideoInfo(
        id=uuid4(),
        youtube_id="abcdefghijk",
        title="Episode",
        # Uploaded in June; the stream happened in May.
        uploaded_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
        broadcast_date=date(2026, 5, 31),
    )
    moments = [
        SearchMoment(id=index, video_id=video.id, start_ms=index * 1000, end_ms=index * 1000 + 500, snippet="rent")
        for index in (1, 2)
    ]
    monkeypatch.setattr(
        "app.archive.product_intelligence.crud.get_grouped_search",
        lambda *_args, **_kwargs: SimpleNamespace(
            groups=[SimpleNamespace(video=video, moments=moments)], total_moments=len(moments)
        ),
    )

    result = build_topic_timeline(
        object(), slug="housing-costs", granularity="month", date_from=date(2026, 5, 1), date_to=None
    )

    assert result.topic == "housing costs"
    assert result.buckets[0].period == "2026-05"
    assert result.buckets[0].mention_count == 2
    assert result.buckets[0].episode_count == 1
    assert result.buckets[0].evidence[0].video.id == video.id


class FakeMappings:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


def test_related_episodes_exposes_explainable_reasons(monkeypatch):
    related_id = uuid4()
    video = VideoInfo(id=related_id, youtube_id="abcdefghijk", title="Related")
    monkeypatch.setattr("app.archive.product_intelligence.crud.get_videos_by_ids", lambda *_args: [video])
    db = SimpleNamespace(
        execute=lambda *_args, **_kwargs: FakeMappings(
            [{"video_id": related_id, "score": 2.0, "reasons": ["Shared tag: Politics"]}]
        )
    )
    result = related_episodes(db, video_id=uuid4())
    assert result.items[0].video.id == related_id
    assert result.items[0].reasons == ["Shared tag: Politics"]


def test_quoted_moments_are_ranked_aggregate_citations():
    video_id = uuid4()
    db = SimpleNamespace(
        execute=lambda *_args, **_kwargs: FakeMappings(
            [{"start_ms": 1000, "end_ms": 2000, "snippet": "quote", "quote_count": 3}]
        )
    )
    result = quoted_moments(db, video_id=video_id)
    assert result.video_id == video_id
    assert result.items[0].quote_count == 3


def test_topic_timeline_pages_past_the_first_result_page(monkeypatch):
    from app.archive import product_intelligence

    video = VideoInfo(id=uuid4(), youtube_id="abcdefghijk", broadcast_date=date(2026, 5, 3))
    offsets = []

    def fake_grouped(*_args, offset=0, limit=0, **_kwargs):
        offsets.append(offset)
        count = limit if len(offsets) < 3 else 1
        moments = [
            SearchMoment(id=offset + i, video_id=video.id, start_ms=i, end_ms=i + 1, snippet="x") for i in range(count)
        ]
        return SimpleNamespace(groups=[SimpleNamespace(video=video, moments=moments)], total_moments=count)

    monkeypatch.setattr(product_intelligence.crud, "get_grouped_search", fake_grouped)
    result = build_topic_timeline(object(), slug="x", granularity="week", date_from=None, date_to=None)

    assert offsets == [0, 500, 1000]
    assert result.buckets[0].mention_count == 1001
    assert result.buckets[0].label == "Week of 2026-04-27"
