"""Stored Whisper word timings are normalized, validated and bounded."""

import json

import pytest

from app.transcripts import words as words_module
from app.transcripts.words import MAX_WORDS_PER_SEGMENT, normalize_words, words_for_segments

# worker/whisper_runner.py faster-whisper path: absolute seconds, leading space, probability.
FASTER_WHISPER = [
    {"word": " Hello", "start": 1.0, "end": 1.42, "probability": 0.97},
    {"word": " world.", "start": 1.42, "end": 2.0, "probability": 0.88},
]
# openai-whisper's own word dicts: times rounded to 10 ms; probability can be absent.
OPENAI_WHISPER = [
    {"word": " Hello", "start": 1.0, "end": 1.4},
    {"word": " world.", "start": 1.4, "end": 2.0, "probability": 0.91},
]


def test_faster_whisper_shape_becomes_millisecond_words():
    assert normalize_words(FASTER_WHISPER, 1000, 2000) == [
        {"start_ms": 1000, "end_ms": 1420, "text": "Hello", "probability": 0.97},
        {"start_ms": 1420, "end_ms": 2000, "text": "world.", "probability": 0.88},
    ]


def test_openai_whisper_shape_without_probability():
    assert normalize_words(OPENAI_WHISPER, 1000, 2000) == [
        {"start_ms": 1000, "end_ms": 1400, "text": "Hello", "probability": None},
        {"start_ms": 1400, "end_ms": 2000, "text": "world.", "probability": 0.91},
    ]


def test_millisecond_text_shape_and_json_string_are_accepted():
    stored = json.dumps([{"text": "hi", "start_ms": 100, "end_ms": 300}])
    assert normalize_words(stored, 0, 500) == [{"start_ms": 100, "end_ms": 300, "text": "hi", "probability": None}]


def test_reversed_out_of_range_and_incomplete_words_are_dropped():
    stored = [
        {"word": "kept", "start": 1.1, "end": 1.2},
        {"word": "reversed", "start": 1.5, "end": 1.3},
        {"word": "early", "start": 0.5, "end": 0.9},
        {"word": "late", "start": 1.9, "end": 2.5},
        {"start": 1.2, "end": 1.3},
        {"word": "  ", "start": 1.2, "end": 1.3},
        {"word": "nan", "start": float("nan"), "end": 1.3},
        {"word": "bool", "start": True, "end": 1.3},
        {"word": "x" * 101, "start": 1.2, "end": 1.3},
        {"word": "odd", "start": 1.2, "end": 1.3, "probability": 7},
    ]
    assert normalize_words(stored, 1000, 2000) == [
        {"start_ms": 1100, "end_ms": 1200, "text": "kept", "probability": None},
        {"start_ms": 1200, "end_ms": 1300, "text": "odd", "probability": None},
    ]


def test_rounding_drift_within_tolerance_is_clamped_to_the_segment():
    stored = [{"word": "edge", "start": 0.98, "end": 2.03}]
    assert normalize_words(stored, 1000, 2000) == [
        {"start_ms": 1000, "end_ms": 2000, "text": "edge", "probability": None}
    ]


@pytest.mark.parametrize(
    "stored",
    [
        None,
        [],
        "not json",
        '{"word": "hi"}',
        {"word": "hi", "start": 0, "end": 1},
        [{"word": "hi", "start": 0.1, "end": 0.2}, "garbage"],
        [{"word": "reversed", "start": 0.4, "end": 0.2}],
        [{"word": "w", "start": 0.0, "end": 0.0}] * (MAX_WORDS_PER_SEGMENT + 1),
    ],
)
def test_missing_or_malformed_rows_have_no_word_timings(stored):
    assert normalize_words(stored, 0, 1000) is None


def test_transcript_status_reports_available_partial_and_unavailable():
    with_words = [0, 1000, "a", None, [{"word": "a", "start": 0.0, "end": 0.5}]]
    without_words = [1000, 2000, "b", None, None]

    assert words_for_segments([with_words])[1:] == ("available", None)
    assert words_for_segments([with_words, without_words])[1:] == ("partial", "not_stored")
    words, status, note = words_for_segments([without_words])
    assert (words, status, note) == ([None], "unavailable", "not_stored")


def test_response_word_limit_returns_later_segments_without_words(monkeypatch):
    monkeypatch.setattr(words_module, "MAX_WORDS_PER_RESPONSE", 3)
    two_words = [{"word": "a", "start": 0.0, "end": 0.1}, {"word": "b", "start": 0.1, "end": 0.2}]
    rows = [[0, 1000, "x", None, two_words], [0, 1000, "y", None, two_words], [0, 1000, "z", None, two_words]]

    words, status, note = words_for_segments(rows)

    assert [len(w) if w else None for w in words] == [2, None, None]
    assert (status, note) == ("partial", "word_limit_reached")
