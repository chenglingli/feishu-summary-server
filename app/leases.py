"""Short transactions fence obsolete workers; external HTTP already in flight cannot be undone."""

import asyncio
from contextvars import ContextVar
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, select

from app.db import Run, SessionLocal, User, UserExecution, now
from app.pipeline import PipelineError
from app.services import ACTIVE

current_lease: ContextVar[tuple[str, str, int] | None] = ContextVar("current_lease", default=None)


# 按用户、任务顺序加锁，领取排队任务并建立用户互斥租约；返回任务 ID、持有者和代次。
# 首次启动设置三十分钟截止时间，重新领取不会延长总执行期限。
def claim(run_id):
    with SessionLocal() as db:
        user_id = db.scalar(select(Run.user_id).where(Run.id == run_id))
        if not user_id:
            return None
        # Consistent order: user -> run -> execution lock. API mutations also lock user first.
        db.scalar(select(User).where(User.id == user_id).with_for_update())
        run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if not run or run.execution_status != "queued":
            return None
        lock = db.get(UserExecution, user_id)
        if lock and lock.lease_until > now():
            return None
        owner = str(uuid4())
        run.fence += 1
        run.lease_owner, run.lease_until = owner, now() + timedelta(seconds=120)
        run.started_at = run.started_at or now()
        run.deadline_at = run.deadline_at or run.started_at + timedelta(minutes=30)
        run.execution_status = "collecting"
        if not lock:
            lock = UserExecution(user_id=user_id)
            db.add(lock)
        lock.run_id, lock.owner, lock.lease_until = run.id, owner, run.lease_until
        db.commit()
        return run.id, owner, run.fence


# 同时校验任务、持有者、fence 代次和租约有效期，排除恢复前的旧执行者。
def owns(run, lease):
    return bool(
        run
        and lease
        and run.id == lease[0]
        and run.lease_owner == lease[1]
        and run.fence == lease[2]
        and run.lease_until
        and run.lease_until > now()
    )


# 确认当前协程仍持有有效租约且未超时，否则禁止继续修改任务状态。
def assert_owner(run):
    if not owns(run, current_lease.get()):
        raise PipelineError("LEASE_LOST")
    if run.deadline_at and run.deadline_at <= now():
        raise PipelineError("TASK_TIMEOUT")


# 每二十秒尝试将任务与用户锁续租一百二十秒；失去所有权或任务终止后退出。
async def heartbeat(lease, stop):
    while not stop.is_set():
        with SessionLocal() as db:
            user_id = db.scalar(select(Run.user_id).where(Run.id == lease[0]))
            db.scalar(select(User).where(User.id == user_id).with_for_update())
            run = db.scalar(select(Run).where(Run.id == lease[0]).with_for_update())
            if not run or not owns(run, lease) or run.execution_status not in ACTIVE:
                return
            lock = db.get(UserExecution, user_id)
            if not lock or lock.owner != lease[1]:
                return
            run.lease_until = lock.lease_until = now() + timedelta(seconds=120)
            db.commit()
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
        except TimeoutError:
            pass


# 仅释放本执行者及代次对应的租约，避免误清除新执行者已经领取的锁。
def release(lease):
    with SessionLocal() as db:
        run = db.get(Run, lease[0])
        if run and run.lease_owner == lease[1] and run.fence == lease[2]:
            run.lease_owner, run.lease_until = None, None
            db.execute(
                delete(UserExecution).where(
                    UserExecution.run_id == run.id, UserExecution.owner == lease[1]
                )
            )
            db.commit()
