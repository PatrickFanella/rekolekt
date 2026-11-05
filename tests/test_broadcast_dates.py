from datetime import date, datetime, timezone

import pytest

from app.archive.broadcast_dates import derive_broadcast_date, parse_title_dates

TODAY = date(2026, 11, 5)


def derive(title, uploaded_at=None, preset="hasanara"):
    return derive_broadcast_date(title, uploaded_at, preset=preset, today=TODAY)


@pytest.mark.parametrize(
    ("title", "uploaded_at", "expected"),
    [
        # VOD uploaded two days after the broadcast: the title wins.
        (
            'HasanAbi November 25, 2024 – "Antisemite of the Year", Trump cases',
            datetime(2024, 11, 27, tzinfo=timezone.utc),
            date(2024, 11, 25),
        ),
        # Archive channel re-upload years later.
        ("2/2 HasanAbi November 28, 2019 – What Would You Do?", datetime(2026, 1, 16), date(2019, 11, 28)),
        # No upload date stored at all.
        ("HasanAbi February 28, 2024 – Mitch McConnell retires", None, date(2024, 2, 28)),
        ("HasanAbi Stream September 8, 2020 - DELETED STREAM FULL VOD", date(2020, 12, 1), date(2020, 9, 8)),
        ("1/2 HasanAbi November 13, 2021 –Adin Ross calls", None, date(2021, 11, 13)),
    ],
)
def test_title_date_beats_upload_date(title, uploaded_at, expected):
    result = derive(title, uploaded_at)
    assert result.date == expected
    assert result.source == "title"
    assert result.kind == "vod"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Day-first numeric dates on HasanAbi channels.
        ("HasanAbi on Dropsite News [04/11/25] – Live at the Zohran victory party", date(2025, 11, 4)),
        ("HasanAbi Clip [14/09/2025] – Hasan visits Caroline Kwan at home!", date(2025, 9, 14)),
        ("[HasanAbi Clip 2/05/2021] Nick from 90 Day Fiancé watches Hasan", date(2021, 5, 2)),
        ("[2025-03-14] WBUR – The stream is up", date(2025, 3, 14)),
        ('Hasan Piker explains himself – Vox "Today, Explained" Podcast, 2025-04-01', date(2025, 4, 1)),
    ],
)
def test_extras_keep_their_dates(title, expected):
    result = derive(title)
    assert result.date == expected
    assert result.kind == "extra"


def test_no_stream_placeholder():
    result = derive("HasanAbi May 5, 2024 – NO STREAM TODAY", date(2024, 5, 6))
    assert result == result.__class__(date(2024, 5, 5), "title", "no_stream")


def test_untitled_video_falls_back_to_upload_day_in_utc():
    result = derive("NEW CHANNEL", datetime(2024, 3, 1, 23, 30, tzinfo=timezone.utc), preset="generic")
    assert (result.date, result.source, result.kind) == (date(2024, 3, 1), "upload_date", "vod")


def test_title_date_after_upload_is_ignored():
    # A typo'd year would place the broadcast after its own upload.
    result = derive("HasanAbi March 3, 2025 –", date(2024, 3, 5))
    assert (result.date, result.source) == (date(2024, 3, 5), "upload_date")


def test_future_title_date_is_ignored():
    assert derive("HasanAbi March 3, 2027 –").date is None


def test_nothing_known():
    result = derive("What a fucking weaselly little liar, dude.")
    assert (result.date, result.source) == (None, None)


def test_generic_preset_reads_numeric_dates_month_first_and_keeps_vods():
    result = derive("Stream 04/11/25 recap", preset="generic")
    assert result.date == date(2025, 4, 11)
    assert result.kind == "vod"


def test_unambiguous_numeric_day_overrides_convention():
    assert parse_title_dates("Clip 25/12/2024", preset="generic")[0].value == date(2024, 12, 25)
    assert parse_title_dates("Clip 12/25/2024", preset="hasanara")[0].value == date(2024, 12, 25)


def test_part_numbers_are_not_dates():
    assert parse_title_dates("1/2 HasanAbi June 9, 2023 – Trump Indictment") == [
        parse_title_dates("HasanAbi June 9, 2023")[0]
    ]


def test_abbreviated_month_and_ordinal():
    assert parse_title_dates("Stream Sept. 3rd 2024")[0].value == date(2024, 9, 3)
    # Words that merely contain a month name are not dates.
    assert parse_title_dates("Mayday 5, 2024") == []


def test_year_first_slash_dates_and_day_ranges():
    assert parse_title_dates("[2023/12/31] Hasan in Italy")[0].value == date(2023, 12, 31)
    result = derive("HasanAbi July 17 & 18, 2022 – NO STREAM BECAUSE HASAN IS IN THE NETHERLANDS STILL")
    assert (result.date, result.kind) == (date(2022, 7, 17), "no_stream")


def test_coverage_counts_each_day_once_and_clips_to_archive_span():
    from app.archive.broadcast_dates import DayIndex, coverage_for_range

    index = DayIndex(
        first_day=date(2024, 1, 3),
        vod_days=frozenset({date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 7)}),
        no_stream_days=frozenset({date(2024, 1, 5)}),
    )
    # January 2024, viewed on January 8: days before the archive began and
    # after today are not counted.
    result = coverage_for_range(index, date(2024, 1, 1), date(2024, 1, 31), today=date(2024, 1, 8))
    assert result == {
        "days": 6,
        "vod_days": 3,
        "no_stream_days": 1,
        "unknown_days": 2,
        "missing_dates": [date(2024, 1, 6), date(2024, 1, 8)],
    }


def test_coverage_outside_archive_span_is_none():
    from app.archive.broadcast_dates import DayIndex, coverage_for_range

    index = DayIndex(date(2024, 1, 3), frozenset({date(2024, 1, 3)}), frozenset())
    assert coverage_for_range(index, date(2023, 12, 1), date(2023, 12, 31), today=date(2024, 2, 1)) is None
    assert coverage_for_range(index, date(2024, 3, 1), date(2024, 3, 31), today=date(2024, 2, 1)) is None
    assert coverage_for_range(DayIndex(None, frozenset(), frozenset()), date(2024, 1, 1), date(2024, 1, 2)) is None
