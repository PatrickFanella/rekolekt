"""Shared admission control for expensive anonymous full-text requests."""

import os
import time
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text

from app.common.session import get_session_token, get_user_from_session
from app.db import engine, get_db
from app.logging_config import get_logger

logger = get_logger(__name__)


def _record_admission(request: Request, caller: str, outcome: str, **details) -> None:
    logger.info(
        "Search admission",
        extra={
            "path": request.url.path.rstrip("/"),
            "caller_class": caller,
            "admission": outcome,
            "request_id": getattr(request.state, "request_id", None),
            "process_id": os.getpid(),
            **details,
        },
    )


# Session advisory locks survive search analytics commits. A dedicated checked-out
# connection owns each slot and must never return a held lock to the pool.
LOCK_NAMESPACE = 1213415762
ANONYMOUS_SEARCH_CONCURRENCY = int(os.environ.get("ANONYMOUS_SEARCH_CONCURRENCY", "2"))
if not 1 <= ANONYMOUS_SEARCH_CONCURRENCY <= 16:
    raise ValueError("ANONYMOUS_SEARCH_CONCURRENCY must be between 1 and 16")

EXPENSIVE_SEARCH_PATHS = frozenset(
    {"/search", "/search/grouped", "/search/mention-map", "/search/export", "/search/mentions/export"}
)


def anonymous_search_budget(request: Request, db=Depends(get_db)) -> Iterator[None]:
    if request.method != "GET" or request.url.path.rstrip("/") not in EXPENSIVE_SEARCH_PATHS:
        yield
        return
    # A cookie or header alone never earns an exemption.
    if get_user_from_session(db, get_session_token(request)):
        _record_admission(request, "authenticated", "exempt")
        yield
        return

    with engine.connect() as connection:
        acquired = None
        started = time.monotonic()
        try:
            for slot in range(ANONYMOUS_SEARCH_CONCURRENCY):
                if connection.execute(
                    text("SELECT pg_try_advisory_lock(:namespace, :slot)"),
                    {"namespace": LOCK_NAMESPACE, "slot": slot},
                ).scalar_one():
                    acquired = slot
                    break
            if acquired is None:
                _record_admission(request, "anonymous", "busy")
                raise HTTPException(
                    status_code=429,
                    detail="Anonymous search is busy. Please retry shortly.",
                    headers={"Retry-After": "10", "Cache-Control": "private, no-store"},
                )
            _record_admission(request, "anonymous", "accepted", slot=acquired)
            yield
        finally:
            if acquired is not None:
                outcome = "released"
                try:
                    # A session lock survives transaction rollback. Recover the
                    # connection before issuing its explicit unlock.
                    connection.rollback()
                    unlocked = connection.execute(
                        text("SELECT pg_advisory_unlock(:namespace, :slot)"),
                        {"namespace": LOCK_NAMESPACE, "slot": acquired},
                    ).scalar_one()
                    if not unlocked:
                        connection.invalidate()
                        outcome = "invalidated"
                except Exception:
                    # Closing the physical connection releases PostgreSQL locks.
                    connection.invalidate()
                    outcome = "invalidated"
                _record_admission(
                    request,
                    "anonymous",
                    outcome,
                    slot=acquired,
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                )
