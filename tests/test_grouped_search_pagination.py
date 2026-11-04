import uuid

from app import crud
from app.schemas import VideoInfo


def test_grouped_search_uses_extra_row_for_offset_pagination(monkeypatch):
    rows = [
        {
            "id": index,
            "video_id": "00000000-0000-0000-0000-000000000001",
            "start_ms": index * 1_000,
            "end_ms": index * 1_000 + 500,
            "snippet": "match",
            "source": "whisper",
        }
        for index in range(3)
    ]
    calls = {}

    def search_rows(*args, **kwargs):
        calls["args"] = args
        calls.update(kwargs)
        return rows

    monkeypatch.setattr(crud, "_search_rows_for_grouping", search_rows)
    monkeypatch.setattr(
        crud,
        "get_videos_by_ids",
        lambda _db, _ids: [
            VideoInfo(id=uuid.UUID("00000000-0000-0000-0000-000000000001"), youtube_id="video-1", title="A VOD")
        ],
    )

    result = crud.get_grouped_search(object(), q="match", limit=2, offset=0)

    assert calls["args"][4] == 3
    assert [moment.id for moment in result.groups[0].moments] == [0, 1]
    assert result.total_moments == 2
    assert result.page_info.model_dump() == {
        "limit": 2,
        "offset": 0,
        "has_next_page": True,
        "has_previous_page": False,
        "next_offset": 2,
        "previous_offset": None,
    }


def test_grouped_search_marks_title_matches(monkeypatch):
    rows = [
        {
            "id": 1,
            "video_id": "00000000-0000-0000-0000-000000000001",
            "start_ms": 0,
            "end_ms": 500,
            "snippet": "A VOD about rent",
            "source": "whisper",
            "title_match": 1,
        },
        {
            "id": 2,
            "video_id": "00000000-0000-0000-0000-000000000001",
            "start_ms": 1_000,
            "end_ms": 1_500,
            "snippet": "rent is too high",
            "source": "whisper",
            "title_match": 0,
        },
    ]
    monkeypatch.setattr(crud, "_search_rows_for_grouping", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(
        crud,
        "get_videos_by_ids",
        lambda _db, _ids: [
            VideoInfo(
                id=uuid.UUID("00000000-0000-0000-0000-000000000001"), youtube_id="video-1", title="A VOD about rent"
            )
        ],
    )

    result = crud.get_grouped_search(object(), q="rent", limit=5, offset=0)

    assert [moment.title_match for moment in result.groups[0].moments] == [True, False]
