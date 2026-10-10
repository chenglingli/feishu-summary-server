"""One PostgreSQL-elected runner, two user execution slots, durable task metadata."""

import asyncio
import signal
from datetime import timedelta

from sqlalchemy import delete, select, text

from app import payloads
from app.cards import report_content, report_parts
from app.config import get_settings
from app.db import (
    Audit,
    AuthNotice,
    DeliveryPart,
    Identity,
    ManualRequest,
    OAuthState,
    Run,
    Session,
    SessionLocal,
    User,
    UserExecution,
    WorkerHealth,
    engine,
    now,
)
from app.delivery import deliver
from app.leases import assert_owner, claim, current_lease, heartbeat, owns, release
from app.maintenance import maintain_credentials, notify_run_reauth
from app.metrics import close_step
from app.pipeline import PipelineError, collect, summarize, user_access
from app.services import ACTIVE, create_run, next_schedule


def permitted(db, run):
    cfg = get_settings()
    user = db.get(User, run.user_id)
    identity = db.get(Identity, user.identity_id)
    return (
        user.auth_epoch == run.auth_epoch
        and user.auth_status == "valid"
        and user.enabled_status != "disabled"
        and identity.tenant_key == cfg.allowed_tenant_key
        and identity.open_id in cfg.pilots
        and identity.access_status == "active"
        and run.execution_status in ACTIVE
    )


def stage(run_id, status, **fields):
    with SessionLocal() as db:
        run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if not run or not permitted(db, run):
            raise PipelineError("CANCELLED")
        assert_owner(run)
        close_step(run)
        run.execution_status = status
        run.steps = [*run.steps, {"stage": status, "at": now().isoformat()}]
        for name, value in fields.items():
            setattr(run, name, value)
        db.commit()


def schedule():
    if get_settings().maintenance_mode:
        return
    with SessionLocal() as db:
        users = db.scalars(
            select(User)
            .where(User.enabled_status == "enabled", User.next_run_at <= now())
            .with_for_update(skip_locked=True)
        )
        for user in users:
            cutoff = user.next_run_at
            assert cutoff is not None
            if cutoff < now() - timedelta(days=30):
                cutoff = next_schedule(user.daily_time, now() - timedelta(days=30))
            while cutoff <= now():
                run = create_run(user, "scheduled", cutoff)
                if now() - cutoff > timedelta(hours=2):
                    run.execution_status, run.finished_at = "missed", now()
                elif user.auth_status != "valid":
                    run.execution_status, run.finished_at = "needs_reauth", now()
                db.add(run)
                cutoff = next_schedule(user.daily_time, cutoff)
            user.next_run_at = cutoff
        db.commit()


def split_report(content, limit=18000):
    # Bound UTF-8 bytes, not Python character counts. Text payload avoids model-generated markup.
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for char in content:
        length = len(char.encode("utf-8"))
        if size + length > limit:
            pieces.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += length
    if current:
        pieces.append("".join(current))
    return [f"({i + 1}/{len(pieces)})\n{x}" for i, x in enumerate(pieces)]


async def execute(run_id, lease=None):
    lease = lease or claim(run_id)
    if not lease:
        return
    context = current_lease.set(lease)
    stop = asyncio.Event()
    pulse = asyncio.create_task(heartbeat(lease, stop))
    try:
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            assert run is not None
            user = db.get(User, run.user_id)
            assert user is not None
            identity = db.get(Identity, user.identity_id)
            assert identity is not None
            deadline = run.deadline_at
        assert deadline is not None
        async with asyncio.timeout(max(0, (deadline - now()).total_seconds())):
            if run.delivery_parts_count:
                parts = payloads.load(run)
            else:
                stage(run_id, "collecting")
                if get_settings().mock_auth_enabled:
                    counts, issues, messages = {"unique": 3, "parsed": 3}, ["DEMO_DATA_ONLY"], []
                    report = "本地演示：任务已完成。未读取真实聊天，未调用模型，未发送飞书。"
                else:
                    if not get_settings().live_pipeline_enabled:
                        raise PipelineError("POC_REQUIRED")
                    access = await user_access(user.id, run.auth_epoch)
                    messages, counts, issues = await collect(run, identity, access)
                    stage(
                        run_id,
                        "summarizing",
                        collection_counts=counts,
                        coverage_issues=issues,
                        coverage_status="partial"
                        if issues
                        else ("complete" if messages else "no_data"),
                    )
                    report = await summarize(messages)
                coverage = "partial" if issues else ("complete" if messages else "no_data")
                stage(
                    run_id,
                    "ready_to_send",
                    collection_counts=counts,
                    coverage_issues=issues,
                    coverage_status=coverage,
                )
                if get_settings().mock_auth_enabled:
                    stage(run_id, "succeeded", finished_at=now(), safe_error="DEMO_NOT_SENT")
                    return
                content = report_content(run.window_start, run.window_end, counts, issues, report)
                parts = report_parts(content)
            await deliver(run_id, identity.open_id, parts)
            stage(run_id, "succeeded", delivery_status="delivered", finished_at=now())
    except (PipelineError, TimeoutError) as error:
        fail(
            run_id,
            error.code if isinstance(error, PipelineError) else "TASK_TIMEOUT",
            getattr(error, "uncertain", False),
        )
        if isinstance(error, PipelineError) and error.code == "AUTHORIZATION_REQUIRED":
            await notify_run_reauth(run_id)
    except Exception:
        fail(run_id, "INTERNAL_PIPELINE_ERROR", True)
    finally:
        stop.set()
        await pulse
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            terminal = bool(run and run.execution_status not in ACTIVE)
        release(lease)
        current_lease.reset(context)
        if terminal and not get_settings().mock_auth_enabled:
            payloads.remove(run_id)


def fail(run_id, code, uncertain=False):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if not run or run.execution_status not in ACTIVE:
            return
        lease = current_lease.get()
        if lease and not owns(run, lease):
            return
        parts = list(db.scalars(select(DeliveryPart).where(DeliveryPart.run_id == run_id)))
        has_unknown = any(p.status == "sending" for p in parts)
        delivered = any(p.status == "delivered" for p in parts)
        close_step(run)
        run.safe_error, run.finished_at = code, now()
        run.execution_status = "delivery_unknown" if has_unknown else "failed"
        run.delivery_status = (
            "unknown"
            if has_unknown
            else (
                "partial"
                if delivered
                else ("failed" if any(p.first_attempt_at for p in parts) else "not_started")
            )
        )
        if code == "AUTHORIZATION_REQUIRED":
            user = db.get(User, run.user_id)
            if user and user.auth_epoch == run.auth_epoch:
                user.auth_status = "needs_reauth"
            run.execution_status = "needs_reauth"
        if code == "CANCELLED":
            run.execution_status = "cancelled"
        db.commit()


def recover():
    with SessionLocal() as db:
        ids = list(
            db.scalars(
                select(Run.id).where(
                    Run.execution_status.in_(ACTIVE - {"queued"}),
                    (Run.lease_until.is_(None) | (Run.lease_until <= now())),
                )
            )
        )
    for run_id in ids:
        with SessionLocal() as db:
            user_id = db.scalar(select(Run.user_id).where(Run.id == run_id))
            db.scalar(select(User).where(User.id == user_id).with_for_update())
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if (
                not run
                or run.execution_status not in ACTIVE
                or (run.lease_until and run.lease_until > now())
            ):
                continue
            run.fence += 1
            run.lease_owner, run.lease_until = None, None
            db.execute(delete(UserExecution).where(UserExecution.run_id == run.id))
            parts = list(db.scalars(select(DeliveryPart).where(DeliveryPart.run_id == run.id)))
            attempted = any(
                p.first_attempt_at or p.status in ("sending", "delivered") for p in parts
            )
            pending_unknown = any(p.status == "sending" for p in parts)
            confirmed = sum(p.status == "delivered" for p in parts)
            if run.delivery_parts_count and confirmed == run.delivery_parts_count:
                run.execution_status, run.delivery_status, run.finished_at = (
                    "succeeded",
                    "delivered",
                    now(),
                )
            elif run.deadline_at and run.deadline_at <= now():
                run.execution_status = "delivery_unknown" if pending_unknown else "failed"
                run.delivery_status = (
                    "unknown" if pending_unknown else ("partial" if confirmed else "not_started")
                )
                run.safe_error, run.finished_at = "TASK_TIMEOUT", now()
            else:
                try:
                    payloads.load(run)
                    available = True
                except PipelineError:
                    available = False
                if attempted and not available:
                    run.execution_status = "delivery_unknown" if pending_unknown else "failed"
                    run.delivery_status = (
                        "unknown" if pending_unknown else ("partial" if confirmed else "failed")
                    )
                    run.safe_error, run.finished_at = "PAYLOAD_LOST", now()
                else:
                    if not available:
                        db.execute(delete(DeliveryPart).where(DeliveryPart.run_id == run.id))
                        run.delivery_parts_count = 0
                    run.execution_status = "queued"
            db.commit()


def clean():
    with SessionLocal() as db:
        db.execute(delete(Session).where(Session.expires_at < now()))
        db.execute(delete(OAuthState).where(OAuthState.expires_at < now()))
        db.execute(delete(ManualRequest).where(ManualRequest.expires_at < now()))
        db.execute(delete(Audit).where(Audit.created_at < now() - timedelta(days=30)))
        db.execute(delete(AuthNotice).where(AuthNotice.attempted_at < now() - timedelta(days=30)))

        expired = select(Run.id).where(
            Run.scheduled_at < now() - timedelta(days=30), Run.execution_status.not_in(ACTIVE)
        )
        db.execute(delete(ManualRequest).where(ManualRequest.run_id.in_(expired)))
        db.execute(delete(DeliveryPart).where(DeliveryPart.run_id.in_(expired)))
        db.execute(delete(UserExecution).where(UserExecution.run_id.in_(expired)))
        db.execute(delete(Run).where(Run.id.in_(expired)))
        db.commit()


async def main():
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    with engine.connect() as leader:
        if not leader.scalar(text("SELECT pg_try_advisory_lock(743820196)")):
            raise SystemExit("Another runner is already active")
        leader.commit()
        try:
            recover()
            tasks: dict[tuple[str, str], asyncio.Task] = {}
            next_maintenance = now()
            maintenance_task = None
            while not stop.is_set():
                # Loss of the leadership connection stops claiming new tasks.
                leader.execute(text("SELECT 1"))
                leader.commit()
                tasks = {k: v for k, v in tasks.items() if not v.done()}
                schedule()
                recover()
                clean()
                with SessionLocal() as db:
                    db.merge(WorkerHealth(name="runner", heartbeat_at=now()))
                    db.commit()
                if not get_settings().mock_auth_enabled:
                    payloads.clean_expired()
                if now() >= next_maintenance and (
                    maintenance_task is None or maintenance_task.done()
                ):
                    maintenance_task = asyncio.create_task(maintain_credentials())
                    next_maintenance = now() + timedelta(hours=1)
                if not get_settings().maintenance_mode:
                    with SessionLocal() as db:
                        candidates = db.scalars(
                            select(Run)
                            .where(Run.execution_status == "queued")
                            .order_by(Run.scheduled_at)
                        )
                        occupied = {user_id for _, user_id in tasks}
                        for run in candidates:
                            if len(tasks) >= 2:
                                break
                            if run.user_id in occupied:
                                continue
                            if not run or not permitted(db, run):
                                run.execution_status, run.finished_at = "cancelled", now()
                                continue
                            occupied.add(run.user_id)
                            tasks[(run.id, run.user_id)] = asyncio.create_task(execute(run.id))
                        db.commit()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=15)
                except TimeoutError:
                    pass
            if tasks:
                await asyncio.gather(*tasks.values())
            if maintenance_task:
                await maintenance_task
        finally:
            leader.execute(text("SELECT pg_advisory_unlock(743820196)"))
            leader.commit()


if __name__ == "__main__":
    asyncio.run(main())
