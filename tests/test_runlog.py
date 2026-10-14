import io
import json
import logging
from datetime import datetime, timedelta, timezone

from app import runlog
from app.leases import current_lease


def test_log_context_and_allowlist(caplog):
    caplog.set_level(logging.INFO, logger="summary.runner")
    token = current_lease.set(("test-run-1", "owner", 1))
    try:
        runlog.event(
            "collection_progress",
            parsed=12,
            context=3,
            text="secret chat",
            token="secret-token",
            code="bad\nraw exception",
            arbitrary="secret",
        )
    finally:
        current_lease.reset(token)
    record = json.loads(caplog.records[-1].message)
    assert record["run_id"] == "test-run-1" and record["parsed"] == 12
    assert not {"text", "token", "code", "arbitrary"} & record.keys()
    assert "secret" not in caplog.text


def test_configuration_is_idempotent_and_flushes(monkeypatch):
    class Stream(io.StringIO):
        flushed = 0

        def flush(self):
            self.flushed += 1
            super().flush()

    logger = logging.Logger("isolated-runner")
    stream = Stream()
    monkeypatch.setattr(runlog, "logger", logger)
    monkeypatch.setattr("sys.stderr", stream)
    runlog.configure()
    runlog.configure()
    before = datetime.now(timezone.utc)
    runlog.event("runner_ready")
    assert len(logger.handlers) == 1
    assert stream.flushed > 0
    record = json.loads(stream.getvalue())
    assert record["event"] == "runner_ready"
    logged_at = datetime.fromisoformat(record["time"])
    assert logged_at.utcoffset() == timedelta(hours=8)
    assert before <= logged_at <= datetime.now(timezone.utc)


async def test_demo_task_logs_claim_stages_and_completion(clients, caplog):
    from uuid import uuid4

    from app.runner import execute

    caplog.set_level(logging.INFO, logger="summary.runner")
    response = clients["demo-a"].post("/api/me/runs", headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 200
    run_id = response.json()["data"]["run_id"]
    await execute(run_id)
    rows = [json.loads(r.message) for r in caplog.records if r.name == "summary.runner"]
    rows = [r for r in rows if r.get("run_id") == run_id]
    assert rows[0]["event"] == "task_claimed"
    assert any(r.get("stage") == "collecting" for r in rows)
    assert rows[-1]["event"] == "task_finished"
    assert rows[-1]["stage"] == "succeeded"
    assert rows[-1]["duration_ms"] >= 0
