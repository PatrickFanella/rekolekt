"""Characterization tests for worker runtime, queue, and maintenance domains."""

from datetime import datetime, timezone

from worker.loop import WorkerLifecycle, pending_video_claim_sql
from worker.maintenance import ModelUpgradePolicy, requeue_for_model_upgrade, rescue_stuck_videos
from worker.queue import pending_video_claim_sql as queue_claim_sql
from worker.runtime import WorkerLifecycle as RuntimeLifecycle


class _Result:
    def fetchall(self):
        return [("video-1", "small")]


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return _Result()


def test_loop_preserves_runtime_and_queue_facade_symbols():
    assert WorkerLifecycle is RuntimeLifecycle
    assert pending_video_claim_sql is queue_claim_sql


def test_maintenance_rescue_and_model_requeue_preserve_guards():
    connection = _Connection()
    produced_before = datetime(2026, 11, 7, tzinfo=timezone.utc)

    rescue_stuck_videos(connection, after_seconds=900)
    rows = requeue_for_model_upgrade(
        connection,
        policy=ModelUpgradePolicy(target_model="large-v3"),
        produced_before=produced_before,
        limit=5,
    )

    rescue_sql, rescue_params = connection.calls[0]
    model_sql, model_params = connection.calls[1]
    assert "diarization_error NOT LIKE 'canary-%'" in rescue_sql
    assert rescue_params == {"secs": 900}
    assert "diarization_error NOT LIKE 'canary-%'" in model_sql
    assert "LIMIT :limit" in model_sql
    assert "CASE" not in model_sql
    assert model_params == {
        "target_model": "large-v3",
        "completed_state": "completed",
        "produced_before": produced_before,
        "limit": 5,
    }
    assert rows == [("video-1", "small")]


def test_model_requeue_with_no_budget_does_not_query():
    connection = _Connection()

    rows = requeue_for_model_upgrade(
        connection,
        policy=ModelUpgradePolicy(target_model="distil-large-v3.5"),
        produced_before=datetime.now(timezone.utc),
        limit=0,
    )

    assert rows == []
    assert connection.calls == []
