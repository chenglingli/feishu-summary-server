"""One PostgreSQL-elected runner, two user execution slots, durable task metadata."""

import asyncio
import signal
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete, select, text

from app.config import get_settings
from app.db import (
    Audit,
    DeliveryPart,
    Identity,
    ManualRequest,
    OAuthState,
    Run,
    Session,
    SessionLocal,
    User,
    engine,
    now,
)
from app.pipeline import PipelineError, bot_token, collect, send_part, summarize, user_access
from app.security import digest
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


async def execute(run_id):
    try:
        async with asyncio.timeout(1800):
            with SessionLocal() as db:
                run = db.get(Run, run_id)
                assert run is not None
                user = db.get(User, run.user_id)
                assert user is not None
                identity = db.get(Identity, user.identity_id)
                assert identity is not None
            stage(run_id, "collecting")
            if get_settings().mock_auth_enabled:
                counts, issues, messages = {"unique": 3, "parsed": 3}, ["DEMO_DATA_ONLY"], []
                report = "本地演示：任务已完成。未读取真实聊天，未调用模型，未发送飞书。"
            else:
                if not get_settings().live_pipeline_enabled:
                    raise PipelineError("POC_REQUIRED")
                access = await user_access(user.id, run.auth_epoch)
                messages, counts, issues = await collect(run, identity, access)
                stage(run_id, "summarizing")
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
            content = (
                f"个人会话总结\n统计截止：{run.window_end.isoformat()}\n覆盖：{coverage}\n"
                + ("部分信息未能读取或尚未验证完整性。\n" if issues else "")
                + report
            )
            access = await bot_token()
            for index, part in enumerate(split_report(content), 1):
                stage(run_id, "sending", delivery_status="sending")
                key = str(uuid5(NAMESPACE_URL, f"{run_id}:report:{index}"))
                with SessionLocal() as db:
                    db.add(
                        DeliveryPart(run_id=run_id, part=index, uuid=key, payload_hash=digest(part))
                    )
                    db.commit()  # Sending intent must survive a crash before issuing the HTTP request.
                message_id = await send_part(access, identity.open_id, part, key)
                with SessionLocal() as db:
                    saved = db.scalar(select(DeliveryPart).where(DeliveryPart.uuid == key))
                    assert saved is not None
                    saved.status, saved.message_id = "delivered", message_id
                    db.commit()
                await asyncio.sleep(1)
            stage(run_id, "succeeded", delivery_status="delivered", finished_at=now())
    except (PipelineError, TimeoutError) as error:
        fail(
            run_id,
            error.code if isinstance(error, PipelineError) else "TASK_TIMEOUT",
            getattr(error, "uncertain", False),
        )
    except Exception:
        # Never log arbitrary upstream exceptions: they can contain chat content or credentials.
        fail(run_id, "INTERNAL_PIPELINE_ERROR", True)


def fail(run_id, code, uncertain=False):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if not run or run.execution_status not in ACTIVE:
            return
        parts = list(db.scalars(select(DeliveryPart).where(DeliveryPart.run_id == run_id)))
        has_unknown = any(p.status == "sending" for p in parts)
        delivered = any(p.status == "delivered" for p in parts)
        run.safe_error, run.finished_at = code, now()
        run.execution_status = "delivery_unknown" if has_unknown else "failed"
        run.delivery_status = (
            "unknown" if has_unknown else ("partial" if delivered else "not_started")
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
        ids = list(db.scalars(select(Run.id).where(Run.execution_status.in_(ACTIVE - {"queued"}))))
    for run_id in ids:
        fail(run_id, "RUNNER_RESTARTED", True)


def clean():
    with SessionLocal() as db:
        db.execute(delete(Session).where(Session.expires_at < now()))
        db.execute(delete(OAuthState).where(OAuthState.expires_at < now()))
        db.execute(delete(ManualRequest).where(ManualRequest.expires_at < now()))
        db.execute(delete(Audit).where(Audit.created_at < now() - timedelta(days=30)))
        expired = select(Run.id).where(
            Run.scheduled_at < now() - timedelta(days=30), Run.execution_status.not_in(ACTIVE)
        )
        db.execute(delete(ManualRequest).where(ManualRequest.run_id.in_(expired)))
        db.execute(delete(DeliveryPart).where(DeliveryPart.run_id.in_(expired)))
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
            while not stop.is_set():
                # Loss of the leadership connection stops claiming new tasks.
                leader.execute(text("SELECT 1"))
                leader.commit()
                tasks = {k: v for k, v in tasks.items() if not v.done()}
                schedule()
                clean()
                if not get_settings().maintenance_mode:
                    with SessionLocal() as db:
                        candidates = db.scalars(
                            select(Run)
                            .where(Run.execution_status == "queued")
                            .order_by(Run.scheduled_at)
                            .with_for_update(skip_locked=True)
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
                            run.execution_status, run.started_at = "collecting", now()
                            occupied.add(run.user_id)
                            tasks[(run.id, run.user_id)] = asyncio.create_task(execute(run.id))
                        db.commit()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=15)
                except TimeoutError:
                    pass
            if tasks:
                await asyncio.gather(*tasks.values())
        finally:
            leader.execute(text("SELECT pg_advisory_unlock(743820196)"))
            leader.commit()


if __name__ == "__main__":
    asyncio.run(main())
