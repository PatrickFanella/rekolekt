"""Admission, cleanup, authentication and cache isolation regression checks."""

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.schemas import MentionMap
from app.search import budget, mention_cache


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


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.redis = MagicMock()
        self.redis.get.return_value = None
        patch.object(mention_cache, "get_redis_client", return_value=self.redis).start()
        patch.object(mention_cache.settings, "ENABLE_CACHING", True).start()
        self.search = patch.object(mention_cache.crud, "get_mention_map").start()
        self.result = MentionMap(query="topic", total_moments=1, total_videos=1)
        self.search.return_value = self.result
        self.addCleanup(patch.stopall)

    def test_hit_avoids_database_search(self):
        self.redis.get.return_value = self.result.model_dump_json()
        self.assertEqual(mention_cache.cached_mention_map(object(), q="topic"), self.result)
        self.search.assert_not_called()

    def test_cache_key_covers_filters_and_hides_terms(self):
        mention_cache.cached_mention_map(object(), q="private term", filters={"date_from": "2026-01-01"})
        first = self.redis.get.call_args.args[0]
        mention_cache.cached_mention_map(object(), q="private term", filters={"date_from": "2025-01-01"})
        self.assertNotEqual(first, self.redis.get.call_args.args[0])
        self.assertNotIn("private term", first)
        self.assertEqual(self.redis.setex.call_args.args[1], 60)

    def test_redis_failure_falls_back(self):
        self.redis.get.side_effect = RuntimeError("unavailable")
        self.redis.setex.side_effect = RuntimeError("unavailable")
        self.assertEqual(mention_cache.cached_mention_map(object(), q="topic"), self.result)
        self.search.assert_called_once()

    def test_bad_cache_is_recomputed(self):
        self.redis.get.return_value = "broken JSON"
        self.assertEqual(mention_cache.cached_mention_map(object(), q="topic"), self.result)
        self.search.assert_called_once()


if __name__ == "__main__":
    unittest.main()
