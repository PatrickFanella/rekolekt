"""Caching of public, user-independent search results.

Keys include a corpus version, so a new transcript or video edit invalidates
earlier results at once and the TTL only bounds metadata drift (tags, people).
The archive intelligence refresher re-warms every published topic's mention
map each hour, so topic pages are served from this cache.
"""

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import text

from app import crud
from app.cache import get_redis_client
from app.logging_config import get_logger
from app.schemas import GroupedSearchResponse, MentionMap
from app.settings import settings

logger = get_logger(__name__)

ResultT = TypeVar("ResultT", bound=BaseModel)

# Longer than the hourly topic warm-up, so warmed topic pages never expire
# between runs.
RESULT_TTL_SECONDS = 7200

# Primary-key maxima and the newest video edit are index or small-table reads.
_CORPUS_VERSION_SQL = text("""
    SELECT (SELECT max(id) FROM segments),
           (SELECT max(id) FROM youtube_segments),
           (SELECT count(*) FROM videos),
           (SELECT max(updated_at) FROM videos)
""")


def _corpus_version(db) -> str | None:
    try:
        row = db.execute(_CORPUS_VERSION_SQL).one()
    except Exception:
        logger.warning("Search corpus version unavailable; serving uncached results")
        return None
    return hashlib.sha256(json.dumps(list(row), default=str).encode()).hexdigest()[:16]


def _cached(
    db, kind: str, model: type[ResultT], compute: Callable[[], Any], params: dict, refresh: bool = False
) -> ResultT:
    client = get_redis_client() if settings.ENABLE_CACHING else None
    version = _corpus_version(db) if client is not None else None
    if client is None or version is None:
        return model.model_validate(compute())
    # Hash every filter/pagination argument so raw search terms never reach
    # Redis keys or logs. User analytics and history stay outside this cache.
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()
    key = f"search-result:v2:{kind}:{version}:{digest}"
    started = time.monotonic()
    try:
        cached = None if refresh else client.get(key)
        if cached is not None:
            result = model.model_validate_json(cached)
            # Report this request's latency, not the original computation's.
            return result.model_copy(update={"query_time_ms": int((time.monotonic() - started) * 1000)})
    except Exception:
        pass  # Cache failures must not replace valid search results.
    result = model.model_validate(compute())
    try:
        client.setex(key, RESULT_TTL_SECONDS, result.model_dump_json())
    except Exception:
        pass
    return result


def cached_mention_map(db, refresh: bool = False, **params) -> MentionMap:
    return _cached(db, "mention-map", MentionMap, lambda: crud.get_mention_map(db, **params), params, refresh)


def topic_mention_map_params(q: str) -> dict[str, Any]:
    """Arguments the topic page's request (``/search/mention-map?q=<slug>``) produces."""
    return {
        "q": q,
        "source": "best",
        "video_id": None,
        "limit": 50,
        "offset": 0,
        "sort_by": "relevance",
        "filters": {},
        "top_limit": 5,
    }


def warm_topic_mention_maps(db) -> int:
    """Recompute and cache the mention map of every published topic."""
    if not settings.ENABLE_CACHING or get_redis_client() is None:
        return 0
    slugs = db.execute(text("SELECT slug FROM archive_topics WHERE status = 'published' ORDER BY slug")).scalars().all()
    warmed = 0
    for slug in slugs:
        try:
            cached_mention_map(db, refresh=True, **topic_mention_map_params(slug))
            warmed += 1
        except Exception:
            db.rollback()
            logger.warning("Topic mention map warm-up failed", extra={"topic": slug})
    return warmed


def cached_grouped_search(db, **params) -> GroupedSearchResponse:
    return _cached(db, "grouped", GroupedSearchResponse, lambda: crud.get_grouped_search(db, **params), params)
