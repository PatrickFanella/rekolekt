from app.transcripts.youtube_formatting import build_youtube_caption_blocks, format_youtube_caption_text


def test_build_youtube_caption_blocks_filters_music_hallucinations():
    rows = [
        (0, 1000, "నికికికికికికికికికికికికికికికికికికికికికికికి"),
        (1000, 2000, "You"),
        (2000, 3000, "Actual spoken caption"),
        (3000, 4000, "continues here"),
    ]

    blocks = build_youtube_caption_blocks(rows)

    assert len(blocks) == 1
    assert blocks[0].block_index == 0
    assert blocks[0].start_ms == 2000
    assert blocks[0].end_ms == 4000
    assert blocks[0].speaker_label is None
    assert blocks[0].kind == "paragraph"
    assert blocks[0].segment_ids == [2, 3]
    assert "Actual spoken caption" in blocks[0].text
    assert "continues here" in blocks[0].text
    assert "నికికి" not in blocks[0].text
    assert blocks[0].text != "You."


def test_format_youtube_caption_text_returns_paragraph_text():
    rows = [
        (0, 1000, "First caption sentence"),
        (1000, 1500, "second caption sentence"),
        (4000, 5000, "New paragraph starts"),
    ]

    text = format_youtube_caption_text(rows)

    assert "First caption sentence" in text
    assert "second caption sentence" in text
    assert "\n\n" in text
    assert "New paragraph starts" in text


def test_youtube_caption_blocks_capitalize_standalone_i():
    rows = [(0, 1000, "i think i'm right"), (1000, 2000, "and the wiki says i.e.")]

    text = format_youtube_caption_text(rows)

    assert text == "I think I'm right and the wiki says i.e."


def test_youtube_caption_blocks_apply_archive_vocabulary_for_hasanara(monkeypatch):
    rows = [(0, 1000, "Oliver Largan joined")]

    monkeypatch.setattr("worker.vocabulary.settings.ARCHIVE_EDITORIAL_PRESET", "generic")
    assert format_youtube_caption_text(rows) == "Oliver Largan joined."

    monkeypatch.setattr("worker.vocabulary.settings.ARCHIVE_EDITORIAL_PRESET", "hasanara")
    assert format_youtube_caption_text(rows) == "Oliver Larkin joined."


def test_youtube_caption_blocks_keep_thank_you():
    rows = [(0, 1000, "Thank you."), (5000, 6000, "ok")]

    blocks = build_youtube_caption_blocks(rows)

    assert [block.text for block in blocks] == ["Thank you.", "Ok."]
