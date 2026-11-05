"""Custom vocabulary processor for post-transcription corrections.

Applies user-defined terminology replacements to improve transcript accuracy
for domain-specific terms, proper nouns, acronyms, etc.
"""

import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

from app.logging_config import get_logger
from app.settings import settings

logger = get_logger(__name__)


# Corrections that are specific to the HasanAbi archive. They apply only when
# ARCHIVE_EDITORIAL_PRESET is "hasanara". Keep ambiguous terms inside their
# recurring phrase so ordinary uses (for example, "MPs") retain their meaning.
ARCHIVE_VOCABULARY: Dict[str, Any] = {
    "terms": [
        {"pattern": r"pre[- ]new", "replacement": "pre-noon", "case_sensitive": False},
        {
            "pattern": r"I(?:'m| am) a Sompike(?:r)?",
            "replacement": "I'm Hasan Piker",
            "case_sensitive": False,
        },
        {
            "pattern": r"(?<=this )thoughts and I(?:'m| am)(?= broadcast)",
            "replacement": "is the HasanAbi",
            "case_sensitive": False,
        },
        {
            "pattern": r"boys,\s+girls and MPs",
            "replacement": "boys, girls and Enbies",
            "case_sensitive": False,
        },
        {
            "pattern": r"Hassan(?:\s+Hassan)?\s+Habib Piker",
            "replacement": "Hasan HasanAbi Piker",
            "case_sensitive": False,
        },
        {"pattern": r"Oliver Largan", "replacement": "Oliver Larkin", "case_sensitive": False},
        {"pattern": r"Amela Keros", "replacement": "Melat Kiros", "case_sensitive": False},
    ]
}

_CURLY_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'"})


def builtin_vocabularies() -> List[Dict[str, Any]]:
    """Return the built-in vocabularies enabled by the editorial preset."""
    return [ARCHIVE_VOCABULARY] if settings.ARCHIVE_EDITORIAL_PRESET == "hasanara" else []


@lru_cache(maxsize=2)
def _builtin_processor(preset: str) -> Optional["VocabularyProcessor"]:
    return VocabularyProcessor([ARCHIVE_VOCABULARY]) if preset == "hasanara" else None


def apply_builtin_vocabulary(text: str) -> str:
    """Apply the preset's built-in corrections to text (cheap enough per request)."""
    processor = _builtin_processor(settings.ARCHIVE_EDITORIAL_PRESET)
    return processor.process_text(text) if processor else text


class VocabularyProcessor:
    """Processes transcripts with custom vocabulary corrections."""

    def __init__(self, vocabularies: List[Dict[str, Any]]):
        """Initialize with vocabulary definitions.

        Args:
            vocabularies: List of vocabulary dicts with 'terms' field
                         Each term should have: pattern, replacement, case_sensitive
        """
        self.vocabularies = vocabularies
        self._compile_patterns()

    def _compile_patterns(self):
        """Compile regex patterns from vocabulary terms."""
        self.patterns = []
        for vocab in self.vocabularies:
            terms = vocab.get("terms", [])
            for term in terms:
                pattern = term.get("pattern", "")
                replacement = term.get("replacement", "")
                case_sensitive = term.get("case_sensitive", False)

                if not pattern or not replacement:
                    continue

                try:
                    flags = 0 if case_sensitive else re.IGNORECASE
                    # Whole-word matching that also works for patterns starting or
                    # ending in punctuation (for example "U.S.").
                    compiled = re.compile(r"(?<!\w)(?:" + pattern + r")(?!\w)", flags)
                    self.patterns.append((compiled, replacement))
                except re.error as e:
                    logger.warning(
                        "Invalid regex pattern in vocabulary",
                        extra={"pattern": pattern, "error": str(e)},
                    )

    def process_text(self, text: str) -> str:
        """Apply vocabulary corrections to text.

        Args:
            text: Original text

        Returns:
            Corrected text with vocabulary replacements applied
        """
        if not text:
            return text

        if not self.patterns:
            return text

        result = text.translate(_CURLY_APOSTROPHES)
        for pattern, replacement in self.patterns:
            # Replacements are literal text, so backslashes cannot raise re.error.
            result = pattern.sub(lambda _match, value=replacement: value, result)

        return result

    def process_segments(self, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Apply vocabulary corrections to transcript segments.

        Args:
            segments: List of segment dicts with 'text' field

        Returns:
            Segments with corrected text
        """
        corrected = []
        for seg in segments:
            corrected_seg = seg.copy()
            if "text" in corrected_seg:
                original = corrected_seg["text"]
                corrected_text = self.process_text(original)
                corrected_seg["text"] = corrected_text
                if original != corrected_text:
                    logger.debug(
                        "Applied vocabulary correction",
                        extra={"original": original, "corrected": corrected_text},
                    )
            corrected.append(corrected_seg)

        return corrected


def load_vocabularies(conn, user_id: Optional[str], vocabulary_ids: List[str]) -> List[Dict[str, Any]]:
    """Load applicable vocabularies from database.

    Args:
        conn: Database connection
        user_id: Owner whose private vocabularies may be loaded
        vocabulary_ids: Exact vocabulary IDs selected when the job was submitted

    Returns:
        List of vocabulary dicts
    """
    from sqlalchemy import text

    if not vocabulary_ids:
        return []

    vocabularies = (
        conn.execute(
            text(
                "SELECT id, name, terms FROM user_vocabularies "
                "WHERE id = ANY(CAST(:vocabulary_ids AS uuid[])) "
                "AND (is_global = true OR user_id = :user_id)"
            ),
            {"vocabulary_ids": vocabulary_ids, "user_id": user_id},
        )
        .mappings()
        .all()
    )

    selected_ids = {str(vocabulary_id) for vocabulary_id in vocabulary_ids}
    loaded_ids = {str(vocabulary["id"]) for vocabulary in vocabularies}
    if loaded_ids != selected_ids:
        raise ValueError("One or more selected vocabularies are unavailable to the job owner")

    logger.info("Loaded vocabularies", extra={"count": len(vocabularies)})
    return [dict(v) for v in vocabularies]


def apply_vocabulary_corrections(
    conn,
    segments: List[Dict[str, Any]],
    user_id: Optional[str],
    vocabulary_ids: List[str],
) -> List[Dict[str, Any]]:
    """Apply vocabulary corrections to segments.

    Args:
        conn: Database connection
        segments: Transcript segments
        user_id: Owner whose private vocabularies may be loaded
        vocabulary_ids: Exact vocabulary IDs selected for this job

    Returns:
        Corrected segments
    """
    vocabularies = load_vocabularies(conn, user_id, vocabulary_ids)
    processor = VocabularyProcessor([*builtin_vocabularies(), *vocabularies])
    return processor.process_segments(segments)
