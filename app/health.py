"""Metadata-only health snapshot shared by the administrator API and local probe."""

import json
from datetime import timedelta

from sqlalchemy import func, select

from app.config import get_settings
from app.db import Run, SessionLocal, WorkerHealth, now
from app.services import ACTIVE


# 汇总 runner 心跳、排队、超时积压和送达未知数量，返回元数据健康状态。
def snapshot(db):
    current = now()
    worker = db.get(WorkerHealth, "runner")
    lag = max(0, int((current - worker.heartbeat_at).total_seconds())) if worker else None
    queued = db.scalar(
        select(func.count()).select_from(Run).where(Run.execution_status == "queued")
    )
    overdue = db.scalar(
        select(func.count())
        .select_from(Run)
        .where(Run.execution_status.in_(ACTIVE), Run.scheduled_at < current - timedelta(minutes=10))
    )
    unknown = db.scalar(
        select(func.count())
        .select_from(Run)
        .where(Run.delivery_status == "unknown", Run.scheduled_at >= current - timedelta(days=1))
    )
    return {
        "status": "ok"
        if lag is not None and lag <= 90 and not overdue and not unknown
        else "degraded",
        "maintenance": get_settings().maintenance_mode,
        "runner_heartbeat_at": worker.heartbeat_at if worker else None,
        "runner_lag_seconds": lag,
        "queued": queued or 0,
        "overdue": overdue or 0,
        "delivery_unknown_24h": unknown or 0,
    }


# 输出健康快照和进程退出码；数据库异常不输出可能带连接凭证的异常原文。
def main():
    try:
        with SessionLocal() as db:
            result = snapshot(db)
        print(json.dumps(result, default=str))
        return 0 if result["status"] == "ok" else 1
    except Exception:
        # Connection exceptions may include connection strings: never print their text.
        print('{"status":"unavailable"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
