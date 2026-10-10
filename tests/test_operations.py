from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import Audit, SessionLocal, WorkerHealth, now
from app.main import app


def test_admin_audit_records_outcome_without_input(clients):
    admin = clients["demo-admin"]
    assert admin.get("/api/admin/users?q=private-query").status_code == 200
    assert admin.get("/api/admin/runs/private-target").status_code == 404
    assert clients["demo-a"].get("/api/admin/users").status_code == 403
    assert TestClient(app).get("/api/admin/users").status_code == 401
    assert admin.get("/api/admin/private-path").status_code == 404
    with SessionLocal() as db:
        audits = list(db.scalars(select(Audit).order_by(Audit.created_at)))
        assert [a.allowed for a in audits] == [True, False, False, False, False]
        assert audits[1].action == "/api/admin/runs/{run_id}"
        assert audits[-1].action == "/api/admin/unmatched"
        assert all("private" not in a.action for a in audits)
        assert len({a.request_id for a in audits}) == 5


def test_health_requires_admin_and_detects_stale_worker(clients):
    admin = clients["demo-admin"]
    assert clients["demo-a"].get("/api/admin/health").status_code == 403
    assert admin.get("/api/admin/health").json()["data"]["status"] == "degraded"
    with SessionLocal() as db:
        db.add(WorkerHealth(name="runner", heartbeat_at=now()))
        db.commit()
    assert admin.get("/api/admin/health").json()["data"]["status"] == "ok"
    with SessionLocal() as db:
        worker = db.get(WorkerHealth, "runner")
        worker.heartbeat_at = now() - timedelta(seconds=100)
        db.commit()
    assert admin.get("/api/admin/health").json()["data"]["status"] == "degraded"


def test_stage_metrics_are_isolated_and_count_retries(clients):
    from uuid import uuid4

    from app.db import Run
    from app.leases import claim, current_lease
    from app.metrics import retry
    from app.runner import fail, stage

    run_id = (
        clients["demo-a"]
        .post("/api/me/runs", headers={"Idempotency-Key": str(uuid4())})
        .json()["data"]["run_id"]
    )
    lease = claim(run_id)
    context = current_lease.set(lease)
    try:
        stage(run_id, "collecting")
        retry("feishu_read")
        stage(run_id, "summarizing")
        retry("model_repair")
        fail(run_id, "MODEL_OUTPUT_INVALID")
    finally:
        current_lease.reset(context)
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run.retry_count == 2
        assert run.steps[0]["retry_counts"] == {"feishu_read": 1}
        assert run.steps[1]["retry_counts"] == {"model_repair": 1}
        assert all(step["duration_ms"] >= 0 and step["finished_at"] for step in run.steps)
