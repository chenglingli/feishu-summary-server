from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import Run, User, now
from app.security import Problem

ACTIVE = {"queued", "collecting", "summarizing", "ready_to_send", "sending", "retry_wait"}


# 按北京时间计算严格晚于 after 的下一次每日执行时间，返回 UTC 供数据库保存。
def next_schedule(daily_time: str, after: datetime | None = None) -> datetime:
    current = (after or now()).astimezone(ZoneInfo("Asia/Shanghai"))
    hour, minute = map(int, daily_time.split(":"))
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return target.astimezone(ZoneInfo("UTC"))


# 查找当前身份的助理配置；写操作可通过 lock 获取用户行锁。
def owner(db, identity, lock=False) -> User | None:
    query = select(User).where(User.identity_id == identity.id)
    return db.scalar(query.with_for_update() if lock else query)


# 要求业务授权有效且未停用；暂停定时任务不影响手动执行。
def require_authorized(user: User | None):
    if not user or user.auth_status != "valid" or user.enabled_status == "disabled":
        raise Problem(409, "AUTHORIZATION_REQUIRED", "请先完成消息授权并启用助理")


# 取消用户的活动任务；正在发送的请求无法撤回，必须保留送达未知状态。
def cancel_runs(db, user):
    for run in db.scalars(
        select(Run).where(Run.user_id == user.id, Run.execution_status.in_(ACTIVE))
    ):
        # In-flight HTTP cannot be undone; keep uncertainty visible instead of claiming no delivery.
        run.execution_status = (
            "delivery_unknown" if run.delivery_status == "sending" else "cancelled"
        )
        if run.delivery_status == "sending":
            run.delivery_status = "unknown"
        run.finished_at = now()


# 构造截止于 cutoff 的二十四小时任务窗口并快照授权代次；由调用方负责入库。
def create_run(user, kind, cutoff):
    return Run(
        user_id=user.id,
        kind=kind,
        scheduled_at=cutoff,
        window_start=cutoff - timedelta(hours=24),
        window_end=cutoff,
        auth_epoch=user.auth_epoch,
        config_version=user.config_version,
    )


# 生成个人设置响应；尚未创建助理配置时提供默认值。
def settings_dto(user):
    return {
        "authorization": user.auth_status if user else "not_connected",
        "enabled_status": user.enabled_status if user else "not_enabled",
        "daily_time": user.daily_time if user else "19:00",
        "timezone": "Asia/Shanghai",
        "next_run_at": user.next_run_at if user else None,
    }


# 生成任务元数据视图，详情额外包含阶段和计数，不包含消息正文或总结内容。
def run_dto(run, identity, detail=False):
    result = {
        "run_id": run.id,
        "user": {"name": identity.name, "open_id": identity.open_id},
        "kind": run.kind,
        "scheduled_at": run.scheduled_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "window_start": run.window_start,
        "window_end": run.window_end,
        "execution_status": run.execution_status,
        "coverage_status": run.coverage_status,
        "delivery_status": run.delivery_status,
        "duration_ms": int(((run.finished_at or now()) - run.started_at).total_seconds() * 1000)
        if run.started_at
        else None,
    }
    if detail:
        result.update(
            steps=run.steps,
            collection_counts=run.collection_counts,
            coverage_issues=run.coverage_issues,
            retry_count=run.retry_count,
            safe_error=run.safe_error,
            delivery_parts=[],
        )
    return result
