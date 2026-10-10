from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app import payloads
from app.db import DeliveryPart, Run, SessionLocal, UserExecution, now
from app.delivery import deliver
from app.leases import assert_owner, claim, current_lease, release
from app.pipeline import PipelineError
from app.runner import recover


def enqueue(clients):
    response = clients["demo-a"].post("/api/me/runs", headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 200
    return response.json()["data"]["run_id"]


def expire(run_id):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        run.lease_until = now() - timedelta(seconds=1)
        lock = db.get(UserExecution, run.user_id)
        if lock:
            lock.lease_until = run.lease_until
        db.commit()


def test_expired_owner_cannot_write_after_reclaim(clients):
    run_id = enqueue(clients)
    old = claim(run_id)
    assert claim(run_id) is None
    expire(run_id)
    recover()
    new = claim(run_id)
    assert new and new[2] > old[2]
    context = current_lease.set(old)
    try:
        with SessionLocal() as db:
            with pytest.raises(PipelineError) as caught:
                assert_owner(db.get(Run, run_id))
            assert caught.value.code == "LEASE_LOST"
    finally:
        current_lease.reset(context)
    release(old)
    with SessionLocal() as db:
        assert db.get(Run, run_id).lease_owner == new[1]


def test_recovery_keeps_original_deadline(clients):
    run_id = enqueue(clients)
    claim(run_id)
    with SessionLocal() as db:
        original = db.get(Run, run_id).deadline_at
    expire(run_id)
    recover()
    claim(run_id)
    with SessionLocal() as db:
        assert db.get(Run, run_id).deadline_at == original


def test_all_confirmed_parts_finish_without_payload(clients):
    run_id = enqueue(clients)
    claim(run_id)
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        run.delivery_parts_count = 1
        db.add(
            DeliveryPart(
                run_id=run_id,
                part=1,
                uuid=str(uuid4()),
                payload_hash="fake",
                status="delivered",
                message_id="m1",
                first_attempt_at=now(),
            )
        )
        db.commit()
    expire(run_id)
    recover()
    with SessionLocal() as db:
        assert db.get(Run, run_id).execution_status == "succeeded"
        assert db.get(Run, run_id).delivery_status == "delivered"


def test_payload_user_binding_and_expiry(clients):
    run_id = enqueue(clients)
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        payloads.save(run, ["fake text"])
        assert payloads.load(run) == ["fake text"]
        run.auth_epoch += 1
        with pytest.raises(PipelineError):
            payloads.load(run)


async def test_ambiguous_send_reuses_exact_uuid_and_payload(clients, monkeypatch):
    import app.delivery as module

    run_id = enqueue(clients)
    lease = claim(run_id)
    context = current_lease.set(lease)
    calls = []

    async def token():
        return "fake"

    async def send(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise PipelineError("DELIVERY_UNKNOWN", uncertain=True)
        return "m1"

    async def no_sleep(_):
        pass

    monkeypatch.setattr(module, "bot_token", token)
    monkeypatch.setattr(module, "send_part", send)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    try:
        await deliver(run_id, "demo-a", ["fake text"])
        assert calls[0] == calls[1]
        with SessionLocal() as db:
            part = db.scalar(select(DeliveryPart).where(DeliveryPart.run_id == run_id))
            assert part.status == "delivered"
            assert db.get(Run, run_id).retry_count == 1
    finally:
        current_lease.reset(context)
        release(lease)


def test_collection_progress_throttles_and_rejects_expired_owner(clients, monkeypatch):
    import time

    from app.metrics import CollectionProgress

    run_id = enqueue(clients)
    lease = claim(run_id)
    context = current_lease.set(lease)
    progress = CollectionProgress({})
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    try:
        progress.update(force=True, unique=10, body_read=2)
        progress.update(body_read=3)
        with SessionLocal() as db:
            assert db.get(Run, run_id).collection_counts == {"unique": 10, "body_read": 2}
        progress.update(force=True)
        with SessionLocal() as db:
            assert db.get(Run, run_id).collection_counts["body_read"] == 3
        expire(run_id)
        with pytest.raises(PipelineError):
            progress.update(force=True, body_read=4)
        with SessionLocal() as db:
            assert db.get(Run, run_id).collection_counts["body_read"] == 3
    finally:
        current_lease.reset(context)


async def test_model_failure_retains_completed_collection_counts(clients, monkeypatch):
    import app.runner as runner
    from app.config import get_settings

    run_id = enqueue(clients)
    monkeypatch.setattr(get_settings(), "mock_auth_enabled", False)
    monkeypatch.setattr(get_settings(), "live_pipeline_enabled", True)

    async def access(*args):
        return "fake-token"

    async def collect(*args):
        return [{"text": "fake"}], {"unique": 8, "parsed": 7, "body_read": 8}, []

    async def summarize(*args):
        raise PipelineError("MODEL_OUTPUT_INVALID")

    monkeypatch.setattr(runner, "user_access", access)
    monkeypatch.setattr(runner, "collect", collect)
    monkeypatch.setattr(runner, "summarize", summarize)
    await runner.execute(run_id)
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run.execution_status == "failed"
        assert run.collection_counts == {"unique": 8, "parsed": 7, "body_read": 8}
