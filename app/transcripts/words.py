"""Normalize stored Whisper word timings for transcript responses.

The worker stores each segment's word list in ``segment_words.words`` exactly as
the Whisper backend produced it. Both faster-whisper and openai-whisper emit
``{"word", "start", "end", "probability"}`` with times in absolute seconds;
openai-whisper rounds times to 10 ms and may leave out ``probability``. Older
rows can carry ``text`` instead of ``word`` or millisecond ``start_ms`` and
``end_ms``. Anything else is treated as malformed and never repaired with
invented times.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from typing import Any, Literal

# Whisper segments last at most 30 seconds, so a longer stored list is not a
# real word list.
MAX_WORDS_PER_SEGMENT = 300
# About nine hours of speech at 180 words a minute, or roughly 8 MB of JSON
# before gzip. Segments past the limit are returned without words.
MAX_WORDS_PER_RESPONSE = 100_000
MAX_WORD_TEXT_CHARS = 100
# Segment bounds are truncated to whole milliseconds and openai-whisper rounds
# word times to 10 ms, so a word can sit a few milliseconds outside its segment.
# Words within this tolerance are clamped to the segment; farther ones are dropped.
BOUNDS_TOLERANCE_MS = 50

WordTimingsStatus = Literal["available", "partial", "unavailable"]
WordTimingsNote = Literal["not_stored", "word_limit_reached", "youtube_captions", "merged_transcript"]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _word_times_ms(word: dict) -> tuple[int, int] | None:
    if "start_ms" in word or "end_ms" in word:
        start, end = _number(word.get("start_ms")), _number(word.get("end_ms"))
        scale = 1
    else:
        start, end = _number(word.get("start")), _number(word.get("end"))
        scale = 1000
    if start is None or end is None:
        return None
    # Same truncation the worker uses for segment bounds.
    return int(start * scale), int(end * scale)


def normalize_words(stored: Any, segment_start_ms: int, segment_end_ms: int) -> list[dict] | None:
    """Return ``[{start_ms, end_ms, text, probability}]`` or ``None`` when no usable words exist."""
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except ValueError:
            return None
    if not isinstance(stored, list) or not stored or len(stored) > MAX_WORDS_PER_SEGMENT:
        return None
    words = []
    for item in stored:
        if not isinstance(item, dict):
            return None
        raw_text = item.get("word", item.get("text"))
        times = _word_times_ms(item)
        if not isinstance(raw_text, str) or times is None:
            continue
        word_text = raw_text.strip()
        start_ms, end_ms = times
        if not word_text or len(word_text) > MAX_WORD_TEXT_CHARS or end_ms < start_ms:
            continue
        if start_ms < segment_start_ms - BOUNDS_TOLERANCE_MS or end_ms > segment_end_ms + BOUNDS_TOLERANCE_MS:
            continue
        probability = _number(item.get("probability"))
        words.append(
            {
                "start_ms": min(max(start_ms, segment_start_ms), segment_end_ms),
                "end_ms": max(min(end_ms, segment_end_ms), segment_start_ms),
                "text": word_text,
                "probability": probability if probability is not None and 0 <= probability <= 1 else None,
            }
        )
    return words or None


def words_for_segments(
    rows: Iterable[Sequence[Any]],
) -> tuple[list[list[dict] | None], WordTimingsStatus, WordTimingsNote | None]:
    """Normalize ``(start_ms, end_ms, text, speaker_label, words)`` rows within the response limit."""
    per_segment: list[list[dict] | None] = []
    total = 0
    limit_reached = False
    for row in rows:
        words = None if limit_reached else normalize_words(row[4], row[0], row[1])
        if words is not None and total + len(words) > MAX_WORDS_PER_RESPONSE:
            limit_reached = True
            words = None
        total += len(words or [])
        per_segment.append(words)
    with_words = sum(1 for words in per_segment if words is not None)
    if with_words == 0:
        return per_segment, "unavailable", "word_limit_reached" if limit_reached else "not_stored"
    if with_words == len(per_segment):
        return per_segment, "available", None
    return per_segment, "partial", "word_limit_reached" if limit_reached else "not_stored"
