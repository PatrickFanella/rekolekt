"""Regression tests for transcript cleanup accuracy."""

from worker.formatter import TranscriptFormatter


def _format(texts, **config):
    formatter = TranscriptFormatter(
        config={
            "enabled": True,
            "add_sentence_punctuation": False,
            "capitalize_sentences": False,
            "merge_short_segments": False,
            "segment_by_sentences": False,
            "remove_fillers": False,
            "fix_all_caps": False,
            "detect_hallucinations": False,
            **config,
        }
    )
    segments = [{"text": text, "start": i * 1000, "end": (i + 1) * 1000} for i, text in enumerate(texts)]
    return [segment["text"] for segment in formatter.format_segments(segments)]


def test_hallucination_filter_keeps_short_real_speech():
    assert _format(["ok", "no", "x"], detect_hallucinations=True) == ["ok", "no"]


def test_hallucination_filter_keeps_speech_that_starts_with_repeats():
    texts = ["no no no no that's wrong", "the the the the"]

    assert _format(texts, detect_hallucinations=True) == ["no no no no that's wrong"]


def test_whisper_silence_phrases_can_be_kept_for_caption_input():
    texts = ["Thank you.", "Thanks for watching."]

    assert _format(texts, detect_hallucinations=True) == []
    assert _format(texts, detect_hallucinations=True, whisper_silence_phrases=False) == texts


def test_filler_removal_leaves_no_stray_punctuation():
    texts = ["um, so, uh, yeah", "So, um, I think", "hello, um.", "Ah, that makes sense"]

    assert _format(texts, remove_fillers=True, filler_level=1) == [
        "so, yeah",
        "So, I think",
        "hello.",
        "Ah, that makes sense",
    ]


def test_sound_tags_are_removed_case_insensitively():
    texts = ["[Music] hello [Applause]", "♪ la la la ♪ and then [ laughter ] we talk"]

    assert _format(texts, remove_special_tokens=True, preserve_sound_events=False) == ["hello", "and then we talk"]


def test_all_caps_fix_keeps_political_acronyms():
    assert _format(["THE DNC AND GOP TALKED TO CNN"], fix_all_caps=True) == ["The DNC and GOP talked to CNN"]


def test_sentence_split_last_piece_ends_at_segment_end():
    formatter = TranscriptFormatter(
        config={
            "enabled": True,
            "segment_by_sentences": True,
            "merge_short_segments": False,
            "add_sentence_punctuation": False,
        }
    )

    result = formatter.format_segments([{"text": "Hello world. How are you? I am fine.", "start": 0, "end": 10000}])

    assert len(result) == 3
    assert result[-1]["end"] == 10000
