"""Tests for the backfill_transcript_blocks script."""

from unittest.mock import MagicMock, Mock, patch

from app.transcripts.blocks import FORMATTER_VERSION
from app.transcripts.types import TranscriptSegment
from scripts import backfill_transcript_blocks as backfill

VIDEO_IDS = [
    "00000000-0000-0000-0000-000000000001",
    "00000000-0000-0000-0000-000000000002",
    "00000000-0000-0000-0000-000000000003",
]


def _fake_engine():
    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value = Mock()
    return engine


def _fake_load_batch(_conn, limit, after_id):
    return [{"id": vid, "youtube_id": "yt", "title": "t"} for vid in VIDEO_IDS if vid > after_id][:limit]


def test_batch_query_selects_missing_or_outdated_blocks_after_cursor():
    conn = Mock()

    backfill._load_batch(conn, 50, VIDEO_IDS[0])

    statement, params = conn.execute.call_args.args
    sql = str(statement)
    assert "NOT EXISTS (SELECT 1 FROM transcript_blocks" in sql
    assert "tb.formatter_version <> :formatter_version" in sql
    assert "v.id > CAST(:after_id AS uuid)" in sql
    assert "ORDER BY v.id" in sql
    assert params == {"limit": 50, "after_id": VIDEO_IDS[0], "formatter_version": FORMATTER_VERSION}


def test_backfill_rebuilds_each_video_in_its_own_transaction_across_batches():
    engine = _fake_engine()
    segments = [TranscriptSegment(start_ms=0, end_ms=1000, text="hello there", speaker_label=None)]

    with (
        patch.object(backfill, "_load_batch", side_effect=_fake_load_batch) as load_batch,
        patch.object(backfill, "_load_segments", return_value=segments),
        patch.object(backfill.crud, "replace_transcript_blocks") as replace,
    ):
        assert backfill.main(batch_size=2, engine=engine) == 0

    assert [c.args[1] for c in replace.call_args_list] == VIDEO_IDS
    blocks = replace.call_args_list[0].args[2]
    assert blocks[0].formatter_version == FORMATTER_VERSION
    # Two batch reads (the second batch is short, so no empty read follows) and one transaction per video.
    assert [c.args[2] for c in load_batch.call_args_list] == [backfill._FIRST_UUID, VIDEO_IDS[1]]
    assert engine.begin.call_count == len(load_batch.call_args_list) + len(VIDEO_IDS)


def test_dry_run_advances_the_cursor_and_writes_nothing():
    engine = _fake_engine()
    segments = [TranscriptSegment(start_ms=0, end_ms=1000, text="hello there", speaker_label=None)]

    with (
        patch.object(backfill, "_load_batch", side_effect=_fake_load_batch) as load_batch,
        patch.object(backfill, "_load_segments", return_value=segments),
        patch.object(backfill.crud, "replace_transcript_blocks") as replace,
    ):
        assert backfill.main(batch_size=1, dry_run=True, engine=engine) == 0

    replace.assert_not_called()
    assert load_batch.call_count == len(VIDEO_IDS) + 1
