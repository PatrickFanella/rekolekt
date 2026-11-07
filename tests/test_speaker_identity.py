"""Speaker attribution: admin mapping, enrollment, matching, search filter, outbox and invalidation."""

import csv
import io
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import speakers
from app.archive.video_metadata_repository import create_person
from app.search.orchestrator import SearchOrchestrator
from app.settings import settings
from scripts import evaluate_speaker_matching, match_speakers
from tests.test_archive_routes import _create_completed_video, _create_user_session, _csrf_headers

MODEL = "test-diarization-model"


def _admin(db_session) -> tuple[dict, dict, str]:
    token = _create_user_session(db_session, email=f"admin-{uuid.uuid4().hex[:8]}@example.com")
    user_id = db_session.execute(text("SELECT user_id FROM sessions ORDER BY created_at DESC LIMIT 1")).scalar_one()
    db_session.execute(text("UPDATE users SET role='admin' WHERE id=:id"), {"id": str(user_id)})
    db_session.commit()
    return {"tc_session": token}, _csrf_headers(token), str(user_id)


def _recording(db_session, word: str, centroids: dict[str, list[float]] | None = None):
    """A completed video whose two speakers each say ``word`` twice."""
    video_id = _create_completed_video(
        db_session,
        youtube_id=f"spk{uuid.uuid4().hex[:8]}",
        title=f"Speaker test {word}",
        uploaded_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    segment_ids = {}
    for index, label in enumerate(["Speaker 1", "Speaker 2", "Speaker 1", "Speaker 2"]):
        segment_ids.setdefault(label, []).append(
            db_session.execute(
                text("""
                    INSERT INTO segments (video_id, start_ms, end_ms, text, speaker_label)
                    VALUES (:v, :s, :e, :t, :label) RETURNING id
                """),
                {
                    "v": str(video_id),
                    "s": index * 2000,
                    "e": index * 2000 + 1500,
                    "t": f"{word} said by {label}",
                    "label": label,
                },
            ).scalar_one()
        )
    for label, vector in (centroids or {}).items():
        db_session.execute(
            text("""
                INSERT INTO video_speaker_centroids (video_id, speaker_label, embedding, speech_seconds, model)
                VALUES (:v, :label, :embedding, 3.0, :model)
            """),
            {"v": str(video_id), "label": label, "embedding": vector, "model": MODEL},
        )
    db_session.commit()
    return video_id, segment_ids


def _person(db_session, name: str) -> dict:
    person = create_person(db_session, {"display_name": f"{name} {uuid.uuid4().hex[:6]}"})
    db_session.commit()
    return person


def _speaker_url(video_id, label="Speaker 1") -> str:
    return f"/admin/videos/{video_id}/speakers/{quote(label)}"


def _outbox_payloads(db_session, segment_ids: list[int]) -> list:
    rows = db_session.execute(
        text("""
            SELECT DISTINCT ON (document_id) document_id, payload FROM search_index_outbox
            WHERE source = 'native' AND document_id = ANY(:ids)
            ORDER BY document_id, id DESC
        """),
        {"ids": segment_ids},
    ).all()
    return [row.payload for row in rows]


def _search_ids(client: TestClient, path: str, q: str, slug: str) -> set[str]:
    response = client.get(path, params={"q": q, "source": "best", "speaker": slug})
    assert response.status_code == 200, response.text
    data = response.json()
    if "hits" in data:
        return {hit["video_id"] + ":" + str(hit["start_ms"]) for hit in data["hits"]}
    return {f"{group['video']['id']}:{m['start_ms']}" for group in data["groups"] for m in group["moments"]}


def test_speaker_routes_require_an_administrator(client: TestClient, db_session):
    video_id, _ = _recording(db_session, "authword")
    reference_body = {"video_id": str(video_id), "speaker_label": "Speaker 1", "segment_ids": [1]}
    assert client.get(f"/admin/videos/{video_id}/speakers").status_code == 401
    assert client.put(_speaker_url(video_id), json={"person_slug": "x"}).status_code == 401
    assert client.delete(_speaker_url(video_id)).status_code == 401
    assert client.get("/admin/archive/metadata/people/x/voice-references").status_code == 401
    assert client.post("/admin/archive/metadata/people/x/voice-references", json=reference_body).status_code == 401

    token = _create_user_session(db_session, email=f"user-{uuid.uuid4().hex[:8]}@example.com")
    cookies, headers = {"tc_session": token}, _csrf_headers(token)
    assert client.get(f"/admin/videos/{video_id}/speakers", cookies=cookies).status_code == 403
    assert (
        client.put(_speaker_url(video_id), json={"person_slug": "x"}, cookies=cookies, headers=headers).status_code
        == 403
    )
    assert client.delete(_speaker_url(video_id), cookies=cookies, headers=headers).status_code == 403
    assert (
        client.post(
            "/admin/archive/metadata/people/x/voice-references", json=reference_body, cookies=cookies, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/admin/archive/metadata/people/x/voice-references/{uuid.uuid4()}", cookies=cookies, headers=headers
        ).status_code
        == 403
    )
    assert (
        db_session.execute(
            text("SELECT count(*) FROM video_speaker_people WHERE video_id=:v"), {"v": str(video_id)}
        ).scalar_one()
        == 0
    )


def test_map_reindexes_filters_search_and_shows_the_person(client: TestClient, db_session):
    cookies, headers, admin_id = _admin(db_session)
    video_id, segments = _recording(db_session, "zephyrmap")
    guest = _person(db_session, "Guest")

    response = client.put(
        _speaker_url(video_id, "Speaker 2"), json={"person_slug": guest["slug"]}, cookies=cookies, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["person"]["slug"] == guest["slug"]
    assert response.json()["reindexed_segments"] == 2

    mapping = db_session.execute(
        text("SELECT method, score, set_by FROM video_speaker_people WHERE video_id=:v AND speaker_label='Speaker 2'"),
        {"v": str(video_id)},
    ).one()
    assert (mapping.method, mapping.score, str(mapping.set_by)) == ("manual", None, admin_id)
    audit = db_session.execute(
        text("SELECT details FROM audit_logs WHERE resource_id = :r ORDER BY created_at DESC LIMIT 1"),
        {"r": f"{video_id}:Speaker 2"},
    ).scalar_one()
    assert audit["operation"] == "speaker_mapping_set" and audit["person_slug"] == guest["slug"]
    assert [p["speaker_person"] for p in _outbox_payloads(db_session, segments["Speaker 2"])] == [guest["slug"]] * 2

    expected = {f"{video_id}:2000", f"{video_id}:6000"}
    assert _search_ids(client, "/search", "zephyrmap", guest["slug"]) == expected
    assert _search_ids(client, "/search/grouped", "zephyrmap", guest["slug"]) == expected
    mention_map = client.get(
        "/search/mention-map", params={"q": "zephyrmap", "source": "native", "speaker": guest["slug"]}
    )
    assert mention_map.status_code == 200
    assert mention_map.json()["total_moments"] == 2
    assert _search_ids(client, "/search", "zephyrmap", "nobody-at-all") == set()

    transcript = client.get(f"/videos/{video_id}/transcript").json()
    people = [segment.get("speaker_person") for segment in transcript["segments"]]
    assert people[0] is None  # Speaker 1 is unmapped: the field is omitted and the raw label stays
    assert people[1] == {"slug": guest["slug"], "display_name": guest["display_name"]}
    assert transcript["segments"][1]["speaker_label"] == "Speaker 2"
    formatted = client.get(f"/videos/{video_id}/transcript", params={"mode": "formatted"}).json()
    assert any(block.get("speaker_person") for block in formatted["blocks"])

    listing = client.get(f"/admin/videos/{video_id}/speakers", cookies=cookies).json()
    by_label = {item["speaker_label"]: item for item in listing["items"]}
    assert by_label["Speaker 2"]["segment_count"] == 2
    assert len(by_label["Speaker 2"]["samples"]) == 2
    assert by_label["Speaker 2"]["mapping"]["status"] == "active"
    assert by_label["Speaker 1"]["mapping"] is None


def test_clear_unmaps_reindexes_and_blocks_automatic_refill(client: TestClient, db_session):
    cookies, headers, _ = _admin(db_session)
    video_id, segments = _recording(db_session, "quillclear", {"Speaker 1": [1.0, 0.0]})
    host = _person(db_session, "Host")
    other_video, _ = _recording(db_session, "quillref", {"Speaker 1": [1.0, 0.0]})
    db_session.execute(
        text("""INSERT INTO speaker_voice_references (person_id, video_id, speaker_label, segment_ids, embedding, model)
                VALUES (:p, :v, 'Speaker 1', ARRAY[1]::bigint[], ARRAY[1.0, 0.0]::real[], :m)"""),
        {"p": str(host["id"]), "v": str(other_video), "m": MODEL},
    )
    client.put(_speaker_url(video_id), json={"person_slug": host["slug"]}, cookies=cookies, headers=headers)

    response = client.delete(_speaker_url(video_id), cookies=cookies, headers=headers)
    assert response.status_code == 200
    assert response.json()["person"] is None
    assert [p["speaker_person"] for p in _outbox_payloads(db_session, segments["Speaker 1"])] == [None, None]
    assert _search_ids(client, "/search", "quillclear", host["slug"]) == set()
    assert client.get(f"/videos/{video_id}/transcript").json()["segments"][0].get("speaker_person") is None
    listing = client.get(f"/admin/videos/{video_id}/speakers", cookies=cookies).json()
    assert listing["items"][0]["mapping"]["status"] == "cleared"
    assert listing["items"][0]["suggestion"]["slug"] == host["slug"]
    assert speakers.auto_match_video(db_session, video_id, threshold=0.5) == []


@pytest.mark.parametrize("change", ["relabel", "retranscribe"])
def test_reprocessing_invalidates_mappings_visibly(client: TestClient, db_session, change):
    cookies, headers, _ = _admin(db_session)
    video_id, segments = _recording(db_session, f"orbit{change}")
    guest = _person(db_session, "Guest")
    client.put(
        _speaker_url(video_id, "Speaker 2"), json={"person_slug": guest["slug"]}, cookies=cookies, headers=headers
    )

    if change == "relabel":
        # Rediarization rewrites labels through the least-privilege worker.
        db_session.execute(
            text("UPDATE segments SET speaker_label = 'Speaker 3' WHERE id = :id"), {"id": segments["Speaker 1"][0]}
        )
        assert [p["speaker_person"] for p in _outbox_payloads(db_session, segments["Speaker 2"])] == [None, None]
    else:
        db_session.execute(text("DELETE FROM segments WHERE video_id = :v"), {"v": str(video_id)})
        db_session.execute(
            text("""INSERT INTO segments (video_id, start_ms, end_ms, text, speaker_label)
                    VALUES (:v, 0, 1000, 'orbitretranscribe again', 'Speaker 2')"""),
            {"v": str(video_id)},
        )
    db_session.commit()

    row = db_session.execute(
        text("SELECT invalidated_at, invalidated_reason FROM video_speaker_people WHERE video_id=:v"),
        {"v": str(video_id)},
    ).one()
    assert row.invalidated_at is not None and row.invalidated_reason
    listing = client.get(f"/admin/videos/{video_id}/speakers", cookies=cookies).json()
    assert {item["speaker_label"]: (item["mapping"] or {}).get("status") for item in listing["items"]}[
        "Speaker 2"
    ] == "stale"
    assert all(s.get("speaker_person") is None for s in client.get(f"/videos/{video_id}/transcript").json()["segments"])
    assert _search_ids(client, "/search", f"orbit{change}", guest["slug"]) == set()

    # Mapping the label again makes it current.
    client.put(
        _speaker_url(video_id, "Speaker 2"), json={"person_slug": guest["slug"]}, cookies=cookies, headers=headers
    )
    assert (
        db_session.execute(
            text("SELECT invalidated_at FROM video_speaker_people WHERE video_id=:v"), {"v": str(video_id)}
        ).scalar_one()
        is None
    )


def test_unknown_video_label_or_person_is_not_found(client: TestClient, db_session):
    cookies, headers, _ = _admin(db_session)
    video_id, _ = _recording(db_session, "nfword")
    person = _person(db_session, "Someone")
    assert client.get(f"/admin/videos/{uuid.uuid4()}/speakers", cookies=cookies).status_code == 404
    assert (
        client.put(
            _speaker_url(video_id, "Speaker 9"), json={"person_slug": person["slug"]}, cookies=cookies, headers=headers
        ).status_code
        == 404
    )
    assert (
        client.put(
            _speaker_url(video_id), json={"person_slug": "no-such-person"}, cookies=cookies, headers=headers
        ).status_code
        == 404
    )


def test_enrollment_stores_the_label_embedding_and_validates_segments(client: TestClient, db_session):
    cookies, headers, _ = _admin(db_session)
    video_id, segments = _recording(db_session, "enrollword", {"Speaker 1": [0.6, 0.8]})
    person = _person(db_session, "Voice")
    url = f"/admin/archive/metadata/people/{person['slug']}/voice-references"

    wrong_label = {"video_id": str(video_id), "speaker_label": "Speaker 1", "segment_ids": segments["Speaker 2"]}
    assert client.post(url, json=wrong_label, cookies=cookies, headers=headers).status_code == 422
    no_embedding = {"video_id": str(video_id), "speaker_label": "Speaker 2", "segment_ids": segments["Speaker 2"]}
    assert client.post(url, json=no_embedding, cookies=cookies, headers=headers).status_code == 422

    body = {"video_id": str(video_id), "speaker_label": "Speaker 1", "segment_ids": segments["Speaker 1"]}
    created = client.post(url, json=body, cookies=cookies, headers=headers)
    assert created.status_code == 201, created.text
    stored = db_session.execute(
        text("SELECT embedding, model, segment_ids FROM speaker_voice_references WHERE id = :id"),
        {"id": created.json()["id"]},
    ).one()
    assert stored.embedding == pytest.approx([0.6, 0.8]) and stored.model == MODEL
    assert sorted(stored.segment_ids) == sorted(segments["Speaker 1"])
    assert [item["id"] for item in client.get(url, cookies=cookies).json()["items"]] == [created.json()["id"]]

    assert client.delete(f"{url}/{created.json()['id']}", cookies=cookies, headers=headers).status_code == 204
    assert client.get(url, cookies=cookies).json()["items"] == []

    # A confirmed mapping can enroll the voice in the same request.
    enrolled = client.put(
        _speaker_url(video_id), json={"person_slug": person["slug"], "enroll": True}, cookies=cookies, headers=headers
    )
    assert enrolled.status_code == 200
    assert len(client.get(url, cookies=cookies).json()["items"]) == 1


def test_automatic_matching_writes_only_above_the_threshold_and_never_over_manual(db_session):
    host = _person(db_session, "Host")
    guest = _person(db_session, "Guest")
    reference_video, _ = _recording(db_session, "refword", {"Speaker 1": [1.0, 0.0], "Speaker 2": [0.0, 1.0]})
    for person, label in ((host, "Speaker 1"), (guest, "Speaker 2")):
        db_session.execute(
            text(
                """INSERT INTO speaker_voice_references (person_id, video_id, speaker_label, segment_ids, embedding, model)
                    SELECT :p, :v, :label, ARRAY[1]::bigint[], embedding, model FROM video_speaker_centroids
                    WHERE video_id = :v AND speaker_label = :label"""
            ),
            {"p": str(person["id"]), "v": str(reference_video), "label": label},
        )
    # Speaker 1 sounds like the host (0.98); Speaker 2 is only loosely like the guest (0.6).
    video_id, segments = _recording(db_session, "autoword", {"Speaker 1": [0.98, 0.2], "Speaker 2": [0.8, 0.6]})

    written = speakers.auto_match_video(db_session, video_id, threshold=0.9)

    assert [(w["speaker_label"], w["slug"]) for w in written] == [("Speaker 1", host["slug"])]
    row = db_session.execute(
        text("SELECT method, score, set_by FROM video_speaker_people WHERE video_id=:v AND speaker_label='Speaker 1'"),
        {"v": str(video_id)},
    ).one()
    assert row.method == "auto" and row.score == pytest.approx(0.98 / (0.98**2 + 0.2**2) ** 0.5) and row.set_by is None
    assert [p["speaker_person"] for p in _outbox_payloads(db_session, segments["Speaker 1"])] == [host["slug"]] * 2
    assert (
        db_session.execute(
            text("SELECT count(*) FROM video_speaker_people WHERE video_id=:v AND speaker_label='Speaker 2'"),
            {"v": str(video_id)},
        ).scalar_one()
        == 0
    )

    # A manual mapping is never replaced by a later automatic run.
    speakers.set_speaker_person(db_session, video_id, "Speaker 1", guest["slug"], user_id=None)
    assert speakers.auto_match_video(db_session, video_id, threshold=0.0) == [
        {"speaker_label": "Speaker 2", "slug": host["slug"], "score": pytest.approx(0.8)}
    ]
    assert (
        db_session.execute(
            text("SELECT method FROM video_speaker_people WHERE video_id=:v AND speaker_label='Speaker 1'"),
            {"v": str(video_id)},
        ).scalar_one()
        == "manual"
    )


def test_match_script_writes_only_when_the_flag_is_on(db_session, monkeypatch):
    host = _person(db_session, "Host")
    reference_video, _ = _recording(db_session, "flagref", {"Speaker 1": [1.0, 0.0]})
    db_session.execute(
        text("""INSERT INTO speaker_voice_references (person_id, video_id, speaker_label, segment_ids, embedding, model)
                VALUES (:p, :v, 'Speaker 1', ARRAY[1]::bigint[], ARRAY[1.0, 0.0]::real[], :m)"""),
        {"p": str(host["id"]), "v": str(reference_video), "m": MODEL},
    )
    video_id, _ = _recording(db_session, "flagword", {"Speaker 1": [1.0, 0.0]})
    monkeypatch.setattr(settings, "SPEAKER_MATCH_THRESHOLD", 0.9)
    count = "SELECT count(*) FROM video_speaker_people WHERE video_id = :v"

    monkeypatch.setattr(settings, "SPEAKER_AUTO_MATCH_ENABLED", False)
    out = io.StringIO()
    assert match_speakers.run(db_session, limit=1000, apply=True, out=out) == 0
    assert "SPEAKER_AUTO_MATCH_ENABLED is false" in out.getvalue()
    assert db_session.execute(text(count), {"v": str(video_id)}).scalar_one() == 0

    monkeypatch.setattr(settings, "SPEAKER_AUTO_MATCH_ENABLED", True)
    assert match_speakers.run(db_session, limit=1000, apply=False, out=io.StringIO()) == 0
    assert db_session.execute(text(count), {"v": str(video_id)}).scalar_one() == 0
    assert match_speakers.run(db_session, limit=1000, apply=True, out=io.StringIO()) >= 1
    assert db_session.execute(text(count), {"v": str(video_id)}).scalar_one() == 1


def test_threshold_evaluation_reports_precision_and_recall():
    labeled = [
        {"video_id": "a", "speaker_label": "Speaker 1", "person_slug": "host"},
        {"video_id": "a", "speaker_label": "Speaker 2", "person_slug": "guest"},
        {"video_id": "b", "speaker_label": "Speaker 1", "person_slug": "host"},
        {"video_id": "b", "speaker_label": "Speaker 2", "person_slug": ""},
    ]
    predictions = [
        {"video_id": "a", "speaker_label": "Speaker 1", "slug": "host", "score": 0.92},
        {"video_id": "a", "speaker_label": "Speaker 2", "slug": "host", "score": 0.71},
        {"video_id": "b", "speaker_label": "Speaker 1", "slug": "host", "score": 0.85},
        {"video_id": "b", "speaker_label": "Speaker 2", "slug": "guest", "score": 0.55},
    ]

    results = {r["threshold"]: r for r in speakers.evaluate_thresholds(labeled, predictions, [0.5, 0.8, 0.95])}

    assert (results[0.5]["predicted"], results[0.5]["correct"]) == (4, 2)
    assert results[0.5]["precision"] == 0.5 and results[0.5]["recall"] == pytest.approx(2 / 3)
    assert results[0.8]["precision"] == 1.0 and results[0.8]["recall"] == pytest.approx(2 / 3)
    assert results[0.95]["precision"] is None and results[0.95]["recall"] == 0.0


def test_evaluation_script_reads_a_sample_and_prints_a_table(tmp_path, db_session):
    host = _person(db_session, "Host")
    reference_video, _ = _recording(db_session, "evalref", {"Speaker 1": [1.0, 0.0]})
    db_session.execute(
        text("""INSERT INTO speaker_voice_references (person_id, video_id, speaker_label, segment_ids, embedding, model)
                VALUES (:p, :v, 'Speaker 1', ARRAY[1]::bigint[], ARRAY[1.0, 0.0]::real[], :m)"""),
        {"p": str(host["id"]), "v": str(reference_video), "m": MODEL},
    )
    video_id, _ = _recording(db_session, "evalword", {"Speaker 1": [0.9, 0.1], "Speaker 2": [0.0, 1.0]})
    sample = tmp_path / "sample.csv"
    with sample.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["video_id", "speaker_label", "person_slug"])
        writer.writerow([str(video_id), "Speaker 1", host["slug"]])
        writer.writerow([str(video_id), "Speaker 2", ""])
        # The reference recording itself must not match its own enrolled voice.
        writer.writerow([str(reference_video), "Speaker 1", host["slug"]])

    labeled = evaluate_speaker_matching.load_sample(sample)
    predictions = evaluate_speaker_matching.predict(db_session, labeled)
    assert all(p["video_id"] != str(reference_video) for p in predictions)
    out = io.StringIO()
    evaluate_speaker_matching.report(speakers.evaluate_thresholds(labeled, predictions, [0.5, 0.995]), labeled, out=out)
    lines = out.getvalue().splitlines()
    assert lines[0] == "labeled speakers: 3 (enrolled people: 2)"
    assert lines[2] == "0.50\t1\t1\t1.000\t0.500"
    assert lines[3] == "0.99\t0\t0\tn/a\t0.000"


def test_opensearch_path_filters_on_the_indexed_speaker_slug(monkeypatch):
    monkeypatch.setattr(settings, "SEARCH_BACKEND", "opensearch")
    response = MagicMock()
    response.json.return_value = {"hits": {"hits": [], "total": {"value": 0}}}
    with (
        patch("app.search.orchestrator.requests.post", return_value=response) as post,
        patch("app.search.orchestrator.search_analytics.record_search_request") as record,
        patch(
            "app.search.orchestrator.search_index_freshness", return_value={"indexed_at": None, "index_lag_seconds": 0}
        ),
    ):
        record.return_value.user_id = None
        try:
            SearchOrchestrator().search(MagicMock(), MagicMock(), q="hello", source="native", speaker="guest-slug")
        except Exception:  # Response shaping beyond the query is not under test here.
            pass
    query = post.call_args.kwargs["json"]
    assert {"term": {"speaker_person": "guest-slug"}} in query["query"]["bool"]["filter"]


def test_search_filter_is_documented_in_openapi(client: TestClient):
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/search", "/search/grouped", "/search/mention-map"):
        assert "speaker" in {parameter["name"] for parameter in paths[path]["get"]["parameters"]}
    assert {"get"} <= set(paths["/admin/videos/{video_id}/speakers"])
    assert {"put", "delete"} <= set(paths["/admin/videos/{video_id}/speakers/{speaker_label}"])
    assert {"get", "post"} <= set(paths["/admin/archive/metadata/people/{slug}/voice-references"])
