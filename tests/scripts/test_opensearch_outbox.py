from scripts.opensearch_indexer import outbox_actions


def test_outbox_actions_use_external_versions_for_upserts_and_deletes(monkeypatch):
    monkeypatch.setattr("scripts.opensearch_indexer.settings.OPENSEARCH_INDEX_NATIVE", "native-index")
    rows = [
        {
            "source": "native",
            "document_id": 10,
            "operation": "upsert",
            "version": 100,
            "payload": {"id": 10, "text": "new"},
        },
        {
            "source": "native",
            "document_id": 11,
            "operation": "delete",
            "version": 101,
            "payload": None,
        },
    ]

    assert outbox_actions(rows) == [
        {
            "index": {
                "_index": "native-index",
                "_id": 10,
                "version": 100,
                "version_type": "external_gte",
            }
        },
        {"id": 10, "text": "new"},
        {
            "delete": {
                "_index": "native-index",
                "_id": 11,
                "version": 101,
                "version_type": "external_gte",
            }
        },
    ]


def test_existing_index_gains_the_speaker_person_keyword_field(monkeypatch):
    from unittest.mock import MagicMock, patch

    from scripts import opensearch_indexer

    monkeypatch.setattr(opensearch_indexer.settings, "OPENSEARCH_URL", "http://search:9200")
    with (
        patch.object(opensearch_indexer.requests, "head", return_value=MagicMock(status_code=200)),
        patch.object(opensearch_indexer.requests, "put", return_value=MagicMock(status_code=200)) as put,
    ):
        opensearch_indexer.ensure_index("native-index")

    put.assert_called_once()
    assert put.call_args.args[0] == "http://search:9200/native-index/_mapping"
    assert put.call_args.kwargs["json"] == {"properties": {"speaker_person": {"type": "keyword"}}}
