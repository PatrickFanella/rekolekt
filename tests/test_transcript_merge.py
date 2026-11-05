from app.transcripts.merged import build_merged_transcript
from app.transcripts.types import TranscriptSegment


def seg(start, end, text, speaker=None):
    return TranscriptSegment(start_ms=start, end_ms=end, text=text, speaker_label=speaker)


def test_merge_uses_whisper_only_when_only_whisper_exists():
    result = build_merged_transcript(
        video_id="video-1",
        whisper_segments=[seg(0, 5000, "hello from whisper", "Speaker 1")],
        youtube_segments=[],
        bucket_ms=5000,
    )

    assert [s.text for s in result.segments] == ["hello from whisper"]
    assert result.blocks[0].primary_source == "whisper"
    assert result.blocks[0].supporting_sources == []


def test_merge_uses_youtube_only_when_only_youtube_exists():
    result = build_merged_transcript(
        video_id="video-1",
        whisper_segments=[],
        youtube_segments=[seg(0, 5000, "hello from youtube")],
        bucket_ms=5000,
    )

    assert [s.text for s in result.segments] == ["hello from youtube"]
    assert result.blocks[0].primary_source == "youtube"


def test_merge_prefers_whisper_and_marks_support_for_similar_text():
    result = build_merged_transcript(
        video_id="video-1",
        whisper_segments=[seg(0, 5000, "the policy discussion continues", "Speaker 1")],
        youtube_segments=[seg(0, 5000, "the policy discussion continues with captions")],
        bucket_ms=5000,
    )

    assert result.segments[0].text == "the policy discussion continues"
    assert result.segments[0].speaker_label == "Speaker 1"
    assert result.blocks[0].primary_source == "whisper"
    assert result.blocks[0].supporting_sources == ["youtube"]
    assert result.blocks[0].needs_review is False
    assert result.blocks[0].merge_reason == "whisper_with_youtube_support"


def test_merge_prefers_whisper_and_flags_heavy_disagreement():
    result = build_merged_transcript(
        video_id="video-1",
        whisper_segments=[seg(0, 5000, "the policy discussion continues", "Speaker 1")],
        youtube_segments=[seg(0, 5000, "rick astley never gonna give you up")],
        bucket_ms=5000,
    )

    assert result.segments[0].text == "the policy discussion continues"
    assert result.segments[0].speaker_label == "Speaker 1"
    assert result.blocks[0].primary_source == "whisper"
    assert result.blocks[0].needs_review is True
    assert result.blocks[0].merge_reason == "heavy_disagreement_prefer_whisper"


def test_merge_skips_youtube_text_inside_a_long_whisper_segment():
    result = build_merged_transcript(
        video_id="video-1",
        whisper_segments=[seg(0, 25000, "a long whisper segment spanning three windows")],
        youtube_segments=[
            seg(0, 5000, "a long whisper"),
            seg(12000, 15000, "segment spanning"),
            seg(21000, 24000, "three windows"),
            seg(30000, 32000, "after the whisper speech"),
        ],
        bucket_ms=10000,
    )

    assert [s.text for s in result.segments] == [
        "a long whisper segment spanning three windows",
        "after the whisper speech",
    ]


def test_merge_corrects_youtube_only_caption_text(monkeypatch):
    monkeypatch.setattr("worker.vocabulary.settings.ARCHIVE_EDITORIAL_PRESET", "hasanara")

    result = build_merged_transcript(
        video_id="video-1",
        whisper_segments=[seg(0, 5000, "hello from whisper")],
        youtube_segments=[seg(20000, 25000, "i think Oliver Largan is right")],
        bucket_ms=5000,
    )

    assert result.segments[1].text == "I think Oliver Larkin is right"
