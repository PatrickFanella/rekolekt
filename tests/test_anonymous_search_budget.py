"""Admission, cleanup, authentication and cache isolation regression checks."""

import logging
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.schemas import GroupedSearchResponse, MentionMap
from app.search import budget, result_cache


def request(path="/search/mention-map", cookie=None):
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request({"type": "http", "method": "GET", "path": path, "headers": headers})


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.engine = patch.object(budget, "engine").start()
        self.engine.connect.return_value = self.connection
        self.auth = patch.object(budget, "get_user_from_session", return_value=None).start()
        self.addCleanup(patch.stopall)

    def test_anonymous_slot_released_after_success(self):
        self.connection.execute.return_value.scalar_one.return_value = True
        admission = budget.anonymous_search_budget(request(), object())
        next(admission)
        self.assertEqual(self.connection.execute.call_count, 1)
        admission.close()
        self.assertIn("pg_advisory_unlock", str(self.connection.execute.call_args.args[0]))

    def test_busy_rejects_without_running_endpoint(self):
        self.connection.execute.return_value.scalar_one.return_value = False
        admission = budget.anonymous_search_budget(request(), object())
        with self.assertRaises(HTTPException) as error:
            next(admission)
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(error.exception.headers["Retry-After"], "10")
        self.assertEqual(self.connection.execute.call_count, budget.ANONYMOUS_SEARCH_CONCURRENCY)

    def test_exception_releases_slot(self):
        self.connection.execute.return_value.scalar_one.return_value = True
        admission = budget.anonymous_search_budget(request(), object())
        next(admission)
        with self.assertRaises(RuntimeError):
            admission.throw(RuntimeError("endpoint failed"))
        self.assertIn("pg_advisory_unlock", str(self.connection.execute.call_args.args[0]))

    def test_failed_unlock_discards_physical_connection(self):
        self.connection.execute.return_value.scalar_one.side_effect = [True, RuntimeError("lost connection")]
        admission = budget.anonymous_search_budget(request(), object())
        next(admission)
        admission.close()
        self.connection.invalidate.assert_called_once()

    def test_invalid_cookie_cannot_bypass(self):
        self.connection.execute.return_value.scalar_one.return_value = False
        with self.assertRaises(HTTPException):
            next(budget.anonymous_search_budget(request(cookie="tc_session=forged"), object()))
        self.assertEqual(self.auth.call_args.args[1], "forged")

    def test_verified_user_exempt(self):
        self.auth.return_value = {"id": "verified-user"}
        admission = budget.anonymous_search_budget(request(cookie="tc_session=valid"), object())
        next(admission)
        admission.close()
        self.engine.connect.assert_not_called()

    def test_cheap_routes_exempt(self):
        for path in ["/search/suggestions", "/search/history", "/ready"]:
            admission = budget.anonymous_search_budget(request(path), object())
            next(admission)
            admission.close()
        self.engine.connect.assert_not_called()
        self.auth.assert_not_called()

    def test_all_expensive_paths_share_budget(self):
        self.connection.execute.return_value.scalar_one.return_value = False
        for path in budget.EXPENSIVE_SEARCH_PATHS:
            with self.assertRaises(HTTPException):
                next(budget.anonymous_search_budget(request(path), object()))


def corpus(version=1):
    db = MagicMock()
    db.execute.return_value.one.return_value = (version, 7, 3, "2026-11-05T00:00:00+00:00")
    return db


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.redis = MagicMock()
        self.redis.get.return_value = None
        patch.object(result_cache, "get_redis_client", return_value=self.redis).start()
        patch.object(result_cache.settings, "ENABLE_CACHING", True).start()
        self.search = patch.object(result_cache.crud, "get_mention_map").start()
        self.result = MentionMap(query="topic", total_moments=1, total_videos=1, query_time_ms=4000)
        self.search.return_value = self.result
        self.addCleanup(patch.stopall)

    def test_hit_avoids_database_search_and_reports_its_own_latency(self):
        self.redis.get.return_value = self.result.model_dump_json()
        hit = result_cache.cached_mention_map(corpus(), q="topic")
        self.assertEqual(hit.total_moments, 1)
        self.assertLess(hit.query_time_ms, 4000)
        self.search.assert_not_called()

    def test_cache_key_covers_filters_and_hides_terms(self):
        result_cache.cached_mention_map(corpus(), q="private term", filters={"date_from": "2026-01-01"})
        first = self.redis.get.call_args.args[0]
        result_cache.cached_mention_map(corpus(), q="private term", filters={"date_from": "2025-01-01"})
        self.assertNotEqual(first, self.redis.get.call_args.args[0])
        self.assertNotIn("private term", first)
        self.assertEqual(self.redis.setex.call_args.args[1], result_cache.RESULT_TTL_SECONDS)

    def test_new_transcripts_change_the_key(self):
        result_cache.cached_mention_map(corpus(1), q="topic")
        first = self.redis.get.call_args.args[0]
        result_cache.cached_mention_map(corpus(2), q="topic")
        self.assertNotEqual(first, self.redis.get.call_args.args[0])

    def test_grouped_and_mention_results_do_not_share_keys(self):
        grouped = patch.object(result_cache.crud, "get_grouped_search").start()
        grouped.return_value = GroupedSearchResponse(total_moments=0, total_videos=0, groups=[])
        result_cache.cached_mention_map(corpus(), q="topic")
        mention_key = self.redis.get.call_args.args[0]
        result_cache.cached_grouped_search(corpus(), q="topic")
        self.assertNotEqual(mention_key, self.redis.get.call_args.args[0])
        grouped.assert_called_once()

    def test_redis_failure_falls_back(self):
        self.redis.get.side_effect = RuntimeError("unavailable")
        self.redis.setex.side_effect = RuntimeError("unavailable")
        self.assertEqual(result_cache.cached_mention_map(corpus(), q="topic"), self.result)
        self.search.assert_called_once()

    def test_unknown_corpus_version_skips_the_cache(self):
        db = MagicMock()
        db.execute.side_effect = RuntimeError("database busy")
        self.assertEqual(result_cache.cached_mention_map(db, q="topic"), self.result)
        self.redis.get.assert_not_called()

    def test_bad_cache_is_recomputed(self):
        self.redis.get.return_value = "broken JSON"
        self.assertEqual(result_cache.cached_mention_map(corpus(), q="topic"), self.result)
        self.search.assert_called_once()


def test_admission_attribution_omits_queries_and_credentials(caplog, monkeypatch):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.scalar_one.return_value = True
    engine = MagicMock()
    engine.connect.return_value = connection
    monkeypatch.setattr(budget, "engine", engine)
    monkeypatch.setattr(budget, "get_user_from_session", lambda *_: None)
    req = request(cookie="tc_session=private-session")
    req.scope["query_string"] = b"q=private-search-text&token=private-token"
    req.state.request_id = "d3ff25d4-d46c-4b13-808a-c8575ab55820"
    with caplog.at_level(logging.INFO, logger=budget.__name__):
        admission = budget.anonymous_search_budget(req, object())
        next(admission)
        admission.close()
    records = [r for r in caplog.records if r.name == budget.__name__]
    assert [r.admission for r in records] == ["accepted", "released"]
    assert all(r.caller_class == "anonymous" and r.path == "/search/mention-map" for r in records)
    assert all(r.request_id == req.state.request_id for r in records)
    serialized = str([r.__dict__ for r in records])
    for secret in ["private-session", "private-search-text", "private-token"]:
        assert secret not in serialized


if __name__ == "__main__":
    unittest.main()
