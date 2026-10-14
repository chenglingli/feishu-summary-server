"""Persist counts/timings only; never attach request data or model output."""

from datetime import datetime

from sqlalchemy import select

from app.db import Run, SessionLocal, now
from app.runlog import event


# 补齐当前未结束阶段的结束时间和耗时；重新赋值 JSON 字段以触发 ORM 更新。
def close_step(run):
    if not run.steps or run.steps[-1].get("finished_at"):
        return
    steps = [dict(step) for step in run.steps]
    end = now()
    steps[-1]["finished_at"] = end.isoformat()
    steps[-1]["duration_ms"] = max(
        0, int((end - datetime.fromisoformat(steps[-1]["at"])).total_seconds() * 1000)
    )
    run.steps = steps


# 校验当前租约后累加任务及阶段重试计数；脱离任务上下文时不落库。
def retry(kind):
    from app.leases import assert_owner, current_lease

    lease = current_lease.get()
    if not lease:
        return
    with SessionLocal() as db:
        run = db.scalar(select(Run).where(Run.id == lease[0]).with_for_update())
        assert_owner(run)
        assert run is not None
        run.retry_count += 1
        if run.steps:
            steps = [dict(step) for step in run.steps]
            counts = dict(steps[-1].get("retry_counts", {}))
            counts[kind] = counts.get(kind, 0) + 1
            steps[-1]["retry_counts"] = counts
            run.steps = steps
        db.commit()
        event("task_retry", code=kind, retry_count=run.retry_count)


class CollectionProgress:
    """Throttle durable counters; only the current leased task may publish progress."""

    # 绑定共享计数字典，初始化进度持久化的节流时间。
    def __init__(self, counts):
        self.counts = counts
        self.last = 0.0

    # 先更新内存计数，通常最多每五秒写库；阶段边界可用 force 强制保存。
    def update(self, *, force=False, **values):
        import time

        from app.leases import assert_owner, current_lease

        self.counts.update(values)
        lease = current_lease.get()
        tick = time.monotonic()
        if not lease or (not force and tick - self.last < 5):
            return
        with SessionLocal() as db:
            run = db.scalar(select(Run).where(Run.id == lease[0]).with_for_update())
            assert_owner(run)
            assert run is not None
            run.collection_counts = dict(self.counts)
            db.commit()
        event("collection_progress", **self.counts)
        self.last = tick
