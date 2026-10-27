"""Exercise admission against PostgreSQL, including a separate API process."""

import os
import selectors
import subprocess
import sys
import time
from hashlib import sha256

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from starlette.requests import Request

from app.logging_config import user_id_ctx
from app.search import budget


def request(path="/search", token=None):
    headers = [(b"cookie", f"tc_session={token}".encode())] if token else []
    return Request({"type": "http", "method": "GET", "path": path, "headers": headers})


def test_slots_are_shared_across_processes_and_released_after_errors_and_death(
    test_engine, test_database_url, monkeypatch
):
    namespace = 1900000000 + os.getpid() % 1000000
    monkeypatch.setattr(budget, "engine", test_engine)
    monkeypatch.setattr(budget, "LOCK_NAMESPACE", namespace)
    monkeypatch.setattr(budget, "ANONYMOUS_SEARCH_CONCURRENCY", 2)
    child_code = """
import sys
from starlette.requests import Request
from app.search import budget
budget.LOCK_NAMESPACE = int(sys.argv[1])
budget.ANONYMOUS_SEARCH_CONCURRENCY = 2
req = Request({'type':'http','method':'GET','path':'/search','headers':[]})
admission = budget.anonymous_search_budget(req, object())
next(admission)
print('admitted', flush=True)
sys.stdin.readline()
admission.close()
"""
    env = {**os.environ, "DATABASE_URL": test_database_url}
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", child_code, str(namespace)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    admission = None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15), "Admission child did not become ready"
        assert child.stdout.readline().strip() == "admitted"
        admission = budget.anonymous_search_budget(request(), object())
        next(admission)
        for path in budget.EXPENSIVE_SEARCH_PATHS:
            with pytest.raises(HTTPException) as error:
                next(budget.anonymous_search_budget(request(path), object()))
            assert error.value.status_code == 429
        with pytest.raises(RuntimeError):
            admission.throw(RuntimeError("cancelled endpoint"))
        admission = budget.anonymous_search_budget(request(), object())
        next(admission)  # The failed endpoint returned its real PostgreSQL slot.
        admission.close()
        admission = None
        child.terminate()
        child.wait(timeout=5)
        with test_engine.connect() as conn:
            deadline = time.monotonic() + 3
            while conn.execute(
                text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND classid=:namespace"),
                {"namespace": namespace},
            ).scalar_one():
                assert time.monotonic() < deadline, "Process death leaked an advisory lock"
                time.sleep(0.01)
        # Both slots are available after connection/process death.
        one = budget.anonymous_search_budget(request(), object())
        two = budget.anonymous_search_budget(request(), object())
        try:
            next(one)
            next(two)
        finally:
            one.close()
            two.close()
    finally:
        if admission is not None:
            admission.close()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        for stream in [child.stdin, child.stdout, child.stderr]:
            stream.close()


def test_only_database_validated_sessions_are_exempt(db_session, test_engine, monkeypatch):
    context_token = user_id_ctx.set(None)
    monkeypatch.setattr(budget, "engine", test_engine)
    monkeypatch.setattr(budget, "LOCK_NAMESPACE", 1901000000 + os.getpid() % 1000000)
    monkeypatch.setattr(budget, "ANONYMOUS_SEARCH_CONCURRENCY", 1)
    db_session.execute(text("CREATE TEMP TABLE users (id text) ON COMMIT DROP"))
    db_session.execute(
        text("CREATE TEMP TABLE sessions (user_id text,token_hash text,expires_at timestamptz) ON COMMIT DROP")
    )
    db_session.execute(text("INSERT INTO users VALUES ('fixture-user')"))
    db_session.execute(
        text("INSERT INTO sessions VALUES ('fixture-user',:hash,now()+interval '1 hour')"),
        {"hash": sha256(b"fixture-session").hexdigest()},
    )
    held = budget.anonymous_search_budget(request(), db_session)
    next(held)
    try:
        with pytest.raises(HTTPException) as error:
            next(budget.anonymous_search_budget(request(token="forged"), db_session))
        assert error.value.status_code == 429
        exempt = budget.anonymous_search_budget(request(token="fixture-session"), db_session)
        next(exempt)
        exempt.close()
    finally:
        held.close()
        user_id_ctx.reset(context_token)
