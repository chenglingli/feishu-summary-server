"""One PostgreSQL-elected runner, two user execution slots, durable task metadata."""

import asyncio
import signal
import time
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
from app.runlog import configure, event
from app.services import ACTIVE, create_run, next_schedule


# 检查任务授权快照和当前访问权限；重新授权、停用或移出试点后，旧任务不可继续。
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


# 持有任务行锁并验证租约后切换阶段，同时结束上一阶段计时并提交进度。
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
        event("task_stage", run_id=run_id, stage=status, coverage=run.coverage_status)


# 将到期计划转换为固定统计窗口的任务，并推进下次执行时间。
# 超过两小时的历史计划只记为错过，避免服务恢复时集中补发旧报告。
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


# 按 UTF-8 字节切分纯文本并添加序号；当前卡片发送流程使用 report_parts。
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


# 执行单个任务：领取租约、采集消息、生成报告、发送固定分段、记录终态。
# 恢复任务复用首次执行的截止时间；已有发送载荷时直接恢复发送，避免重新生成内容。
async def execute(run_id, lease=None):
    lease = lease or claim(run_id)
    if not lease:
        return
    context = current_lease.set(lease)
    started = time.monotonic()
    event("task_claimed")
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
            # 恢复发送必须复用已固定的载荷；只有未准备发送的任务才重新采集与总结。
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
                    # 可交付的部分总结仍进入发送流程，同时在任务覆盖状态中明确标记遗漏。
                    if getattr(report, "incomplete", False):
                        issues = [*issues, "SUMMARY_PARTIAL"]
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
    except Exception as error:
        event("task_exception", error_type=type(error).__name__)
        fail(run_id, "INTERNAL_PIPELINE_ERROR", True)
    finally:
        stop.set()
        await pulse
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            terminal = bool(run and run.execution_status not in ACTIVE)
        release(lease)
        event(
            "task_finished",
            stage=run.execution_status if run else "unknown",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        current_lease.reset(context)
        if terminal and not get_settings().mock_auth_enabled:
            payloads.remove(run_id)


# 仅由当前有效执行者记录失败，并根据分段状态区分未发送、部分送达和结果未知。
def fail(run_id, code, uncertain=False):
    event("task_failed", run_id=run_id, code=code)
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


# 回收租约过期的任务；依据截止时间、分段送达记录和载荷是否仍在决定恢复或终止。
# 已尝试发送却丢失载荷时不可重新总结发送，否则可能重复投递不同内容。
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
            # 先推进执行代次，使旧协程即便恢复运行也无法继续写入任务进度。
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
            # 重启不重置总超时；已经全部确认送达的任务优先判成功，其余超时终止。
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


# 清理过期会话、授权状态和历史任务元数据；保留仍在执行的任务。
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


# 读取后台协程异常防止静默失败，只记录异常类型，不记录可能含敏感数据的异常正文。
def task_done(task):
    if not task.cancelled():
        error = task.exception()
        if error:
            event("background_task_failed", error_type=type(error).__name__)


# 持有 PostgreSQL 会话级锁运行唯一调度器，最多同时处理两个不同用户的任务。
# 循环负责调度、故障恢复、清理和凭证维护；正常退出先等待在途任务完成。
async def main():
    configure()
    event("runner_starting")
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    with engine.connect() as leader:
        if not leader.scalar(text("SELECT pg_try_advisory_lock(743820196)")):
            event("runner_already_active")
            raise SystemExit("Another runner is already active")
        leader.commit()
        event("runner_ready")
        try:
            recover()
            tasks: dict[tuple[str, str], asyncio.Task] = {}
            next_maintenance = now()
            maintenance_task = None
            next_status = 0.0
            while not stop.is_set():
                # Loss of the leadership connection stops claiming new tasks.
                leader.execute(text("SELECT 1"))
                leader.commit()
                tasks = {k: v for k, v in tasks.items() if not v.done()}
                if time.monotonic() >= next_status:
                    event("runner_status", active_tasks=len(tasks))
                    next_status = time.monotonic() + 300
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
                    event("maintenance_started")
                    maintenance_task = asyncio.create_task(maintain_credentials())
                    maintenance_task.add_done_callback(task_done)
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
                            tasks[(run.id, run.user_id)].add_done_callback(task_done)
                        db.commit()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=15)
                except TimeoutError:
                    pass
            event("runner_draining", active_tasks=len(tasks))
            if tasks:
                await asyncio.gather(*tasks.values())
            if maintenance_task:
                await maintenance_task
        finally:
            leader.execute(text("SELECT pg_advisory_unlock(743820196)"))
            leader.commit()
            event("runner_stopped")


if __name__ == "__main__":
    asyncio.run(main())
