from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from app.transcripts.blocks import TranscriptBlock, build_transcript_blocks
from app.transcripts.types import TranscriptSegment
from worker.vocabulary import apply_builtin_vocabulary

YouTubeCaptionRow = tuple[int, int, str]

# Auto captions write the pronoun as "i"; leave abbreviations such as "i.e." alone.
_LOWERCASE_PRONOUN_RE = re.compile(r"(?<![\w'.])i(?![\w.])|(?<![\w'.])i(?=\.(?!\w))")


def correct_caption_text(text: str) -> str:
    """Read-time corrections for YouTube caption text."""
    return _LOWERCASE_PRONOUN_RE.sub("I", apply_builtin_vocabulary(text))


def youtube_rows_to_segments(rows: Iterable[YouTubeCaptionRow]) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(
            start_ms=int(start_ms),
            end_ms=int(end_ms),
            text=str(text or ""),
            speaker_label=None,
        )
        for start_ms, end_ms, text in rows
    ]


def build_youtube_caption_blocks(rows: Sequence[YouTubeCaptionRow]) -> list[TranscriptBlock]:
    segments = youtube_rows_to_segments(
        (start_ms, end_ms, correct_caption_text(str(text or ""))) for start_ms, end_ms, text in rows
    )
    return build_transcript_blocks(segments, whisper_silence_phrases=False)


def format_youtube_caption_text(rows: Sequence[YouTubeCaptionRow]) -> str:
    blocks = build_youtube_caption_blocks(rows)
    return "\n\n".join(block.text for block in blocks if block.text.strip())
