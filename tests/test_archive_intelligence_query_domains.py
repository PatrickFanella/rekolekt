"""Characterization coverage for intelligence repository query facades."""

from datetime import date, datetime, timezone

from app.archive import intelligence_repository as facade
from app.archive.intelligence_queries import periods, topics


def test_topic_facade_preserves_query_domain_functions():
    assert facade.slugify_topic is topics.slugify_topic
    assert facade.alias_matches_text is topics.alias_matches_text
    assert facade.slugify_topic("New Jersey!!!") == "new-jersey"
    assert facade.alias_matches_text("ice", "anti-Semite price") is False


def test_period_facade_preserves_calendar_boundaries():
    value = date(2026, 6, 4)
    assert facade._month_bounds(value) == periods.month_bounds(value)
    assert facade._week_bounds(value) == periods.week_bounds(value)
    assert facade._period_key(datetime(2026, 6, 4, tzinfo=timezone.utc), "week") == "2026-W23"
    assert facade._within_period_range("not-a-period", "month", None, None) is True


def test_period_range_matches_overlapping_periods():
    # A range starting mid-month still includes that month and its week.
    assert periods.within_period_range("2024-10", "month", date(2024, 10, 15), None) is True
    assert periods.within_period_range("2024-W42", "week", date(2024, 10, 16), date(2024, 10, 16)) is True
    assert periods.within_period_range("2024-09", "month", date(2024, 10, 1), None) is False
    assert periods.within_period_range("2024-11", "month", None, date(2024, 10, 31)) is False


def test_bucket_labels_and_bounds():
    assert periods.bucket_period_and_label(date(2024, 10, 9), "week") == ("2024-W41", "Week of 2024-10-07")
    assert periods.bucket_bounds(date(2024, 2, 1), "month") == (date(2024, 2, 1), date(2024, 2, 29))
    assert periods.bucket_bounds(date(2024, 1, 1), "year") == (date(2024, 1, 1), date(2024, 12, 31))


def test_explicit_range_or_week_view_skips_the_named_period(monkeypatch):
    calls = []
    monkeypatch.setattr(facade, "_table_has_rows", lambda _db, table: table.startswith("archive_named_period"))
    monkeypatch.setattr(facade, "_latest_named_period_slug", lambda _db: "2026-10")
    monkeypatch.setattr(
        facade, "get_named_period_intelligence", lambda _db, slug, topic_limit: calls.append(slug) or "named"
    )

    # Range and week requests fall through to calendar buckets (None here: no topic tables).
    assert facade.get_durable_archive_intelligence(object(), date_from=date(2024, 10, 1)) is None
    assert facade.get_durable_archive_intelligence(object(), granularity="week") is None
    assert calls == []
    assert facade.get_durable_archive_intelligence(object()) == "named"
    assert facade.get_durable_archive_intelligence(object(), period_slug="2024-w41") == "named"
    assert calls == ["2026-10", "2024-w41"]
