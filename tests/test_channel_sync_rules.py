import json
from datetime import date

import pytest
from pydantic import ValidationError

from app.channel_sync_rules import entry_published_date, load_sync_channels, parse_channel_sync_config
from app.settings import Settings


def config(**rules):
    return json.dumps({"channels": [{"url": "https://youtube.com/@vods/videos", **rules}]})


def rules_for(**rules):
    return parse_channel_sync_config(config(**rules))[0].rules


def test_channel_sync_urls_alone_are_rules_free_channels():
    channels = load_sync_channels(" https://youtube.com/@one/videos, ,https://youtube.com/@two/videos ", "")

    assert [channel.url for channel in channels] == [
        "https://youtube.com/@one/videos",
        "https://youtube.com/@two/videos",
    ]
    for channel in channels:
        assert channel.rules.skip_reason(title="anything", duration_seconds=None, published=None) is None


def test_rules_file_channels_are_added_after_channel_sync_urls(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(config(min_duration_seconds=60, missing_duration="skip"))

    channels = load_sync_channels("https://youtube.com/@one/videos", str(path))

    assert [channel.url for channel in channels] == [
        "https://youtube.com/@one/videos",
        "https://youtube.com/@vods/videos",
    ]
    assert channels[1].rules.min_duration_seconds == 60


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (config(title_include=["(unclosed"]), r"channels\[0\]: title_include\[0\] is not a valid regular expression"),
        (config(title_exclude=["x" * 201]), r"title_exclude\[0\] must be a pattern of 1 to 200 characters"),
        (config(title_include=[]), "title_include must be a list of 1 to 20 patterns"),
        (
            config(min_duration_seconds=600, max_duration_seconds=60, missing_duration="skip"),
            "min_duration_seconds is greater than max_duration_seconds",
        ),
        (config(min_duration_seconds=-1, missing_duration="skip"), "min_duration_seconds must be a non-negative"),
        (config(max_duration_seconds="60", missing_duration="skip"), "max_duration_seconds must be a non-negative"),
        (config(min_duration_seconds=60), "missing_duration is required"),
        (config(min_duration_seconds=60, missing_duration="maybe"), 'missing_duration must be "include" or "skip"'),
        (config(missing_duration="skip"), "missing_duration has no effect without its rule"),
        (config(published_after="2026-02-30", missing_published_date="skip"), "published_after must be a YYYY-MM-DD"),
        (config(published_after="2026-01-01"), "missing_published_date is required"),
        (config(min_duration=60), "unknown keys: min_duration"),
        (json.dumps({"channels": [], "extra": 1}), "unknown top-level keys: extra"),
        (json.dumps({"channels": [{}]}), "url must be a non-empty string"),
        (json.dumps({"channels": {}}), '"channels" must be a list'),
        ("{not json", "invalid JSON at line 1"),
    ],
)
def test_invalid_rules_are_rejected_with_a_clear_message(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_channel_sync_config(raw)


def test_a_channel_listed_twice_is_rejected(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(config())
    with pytest.raises(ValueError, match="configured more than once"):
        load_sync_channels("https://youtube.com/@vods/videos", str(path))


def test_an_unreadable_rules_file_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="cannot be read"):
        load_sync_channels("", str(tmp_path / "missing.json"))


def test_invalid_rules_fail_settings_startup(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(config(title_exclude=["[unclosed"]))

    with pytest.raises(ValidationError, match="title_exclude"):
        Settings(_env_file=None, CHANNEL_SYNC_CONFIG=str(path))


def test_valid_rules_load_with_settings(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(config(title_exclude=["(?i)#shorts"]))

    assert Settings(_env_file=None, CHANNEL_SYNC_CONFIG=str(path)).CHANNEL_SYNC_CONFIG == str(path)


@pytest.mark.parametrize(
    ("rules", "upload", "reason"),
    [
        (
            {"min_duration_seconds": 600, "missing_duration": "include"},
            {"duration_seconds": 599},
            "min_duration_seconds",
        ),
        ({"min_duration_seconds": 600, "missing_duration": "include"}, {"duration_seconds": 600}, None),
        (
            {"max_duration_seconds": 600, "missing_duration": "include"},
            {"duration_seconds": 601},
            "max_duration_seconds",
        ),
        ({"max_duration_seconds": 600, "missing_duration": "include"}, {"duration_seconds": 600}, None),
        ({"min_duration_seconds": 600, "missing_duration": "include"}, {}, None),
        ({"min_duration_seconds": 600, "missing_duration": "skip"}, {}, "missing_duration"),
        ({"title_include": ["VOD", "Stream"]}, {"title": "Full Stream 11/7"}, None),
        ({"title_include": ["VOD", "Stream"]}, {"title": "Trailer"}, "title_include"),
        ({"title_exclude": ["(?i)#shorts"]}, {"title": "Clip #SHORTS"}, "title_exclude"),
        ({"title_exclude": ["(?i)#shorts"]}, {"title": "Full VOD"}, None),
        (
            {"published_after": "2026-01-01", "missing_published_date": "include"},
            {"published": date(2025, 12, 31)},
            "published_after",
        ),
        ({"published_after": "2026-01-01", "missing_published_date": "include"}, {"published": date(2026, 1, 1)}, None),
        ({"published_after": "2026-01-01", "missing_published_date": "include"}, {}, None),
        ({"published_after": "2026-01-01", "missing_published_date": "skip"}, {}, "missing_published_date"),
    ],
)
def test_each_rule_includes_or_skips_as_documented(rules, upload, reason):
    upload = {"title": "", "duration_seconds": None, "published": None, **upload}
    assert rules_for(**rules).skip_reason(**upload) == reason


def test_published_date_comes_from_timestamps_or_upload_date():
    assert entry_published_date({"timestamp": 1767225600}) == date(2026, 1, 1)
    assert entry_published_date({"release_timestamp": 1767225600, "timestamp": 0}) == date(2026, 1, 1)
    assert entry_published_date({"upload_date": "20251231"}) == date(2025, 12, 31)
    assert entry_published_date({"upload_date": "2025-12-31"}) is None
    assert entry_published_date({}) is None
