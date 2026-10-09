"""Persist counts/timings only; never attach request data or model output."""

from datetime import datetime

from sqlalchemy import select

from app.db import Run, SessionLocal, now


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


class CollectionProgress:
    """Throttle durable counters; only the current leased task may publish progress."""

    def __init__(self, counts):
        self.counts = counts
        self.last = 0.0

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
        self.last = tick
