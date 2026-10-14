"""Credential maintenance is independent of user schedules and browser sessions."""

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.config import get_settings
from app.db import AuthNotice, Identity, SessionLocal, Token, User, now
from app.pipeline import PipelineError, bot_token, send_part, user_access


# 仅将匹配授权代次的有效凭证标记为需重授权，避免覆盖后续的新授权。
def mark_needs_reauth(user_id, epoch):
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.id == user_id).with_for_update())
        if user and user.auth_epoch == epoch and user.auth_status == "valid":
            user.auth_status = "needs_reauth"
            user.auth_checked_at = now()
            db.commit()


# 同一授权代次只记录一次通知意图，再发送固定 UUID 的重授权提醒。
async def notify_reauth(user_id, epoch):
    cfg = get_settings()
    if cfg.mock_auth_enabled or not cfg.live_pipeline_enabled:
        return
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.id == user_id).with_for_update())
        if (
            not user
            or user.auth_epoch != epoch
            or user.auth_status != "needs_reauth"
            or user.enabled_status != "enabled"
        ):
            return
        identity = db.get(Identity, user.identity_id)
        if (
            not identity
            or identity.tenant_key != cfg.allowed_tenant_key
            or identity.open_id not in cfg.pilots
            or identity.access_status != "active"
        ):
            return
        if user.reauth_notice_epoch >= epoch or db.get(AuthNotice, (user_id, epoch)):
            return
        # Persist intent before external I/O. Unknown outcomes never get a fresh notification UUID.
        user.reauth_notice_epoch = epoch
        db.add(AuthNotice(user_id=user_id, auth_epoch=epoch))
        db.commit()
        recipient = identity.open_id
    status, message_id = "failed", None
    try:
        access = await bot_token()
        message_id = await send_part(
            access,
            recipient,
            f"会话总结助手的消息授权已失效，请重新授权后继续使用：{cfg.public_base_url}/",
            str(uuid5(NAMESPACE_URL, f"reauth:{user_id}:{epoch}")),
        )
        status = "delivered"
    except PipelineError as error:
        status = "unknown" if error.uncertain else "failed"
    with SessionLocal() as db:
        notice = db.get(AuthNotice, (user_id, epoch))
        if notice:
            notice.status, notice.message_id = status, message_id
            db.commit()


# 独立于每日任务检查启用用户的凭证，刷新到期令牌并通知需重授权的用户。
async def maintain_credentials():
    cfg = get_settings()
    if cfg.mock_auth_enabled or not cfg.live_pipeline_enabled or cfg.maintenance_mode:
        return
    with SessionLocal() as db:
        users = list(
            db.scalars(
                select(User)
                .join(Identity, User.identity_id == Identity.id)
                .where(
                    User.enabled_status == "enabled",
                    Identity.tenant_key == cfg.allowed_tenant_key,
                    Identity.open_id.in_(cfg.pilots),
                    Identity.access_status == "active",
                )
            )
        )
    for user in users:
        if user.auth_status == "valid":
            with SessionLocal() as db:
                token = db.get(Token, user.id)
                due = not token or token.refresh_after <= now() or token.expires_at <= now()
            if due:
                try:
                    await user_access(user.id, user.auth_epoch)
                except PipelineError as error:
                    if error.code == "AUTHORIZATION_REQUIRED":
                        mark_needs_reauth(user.id, user.auth_epoch)
        await notify_reauth(user.id, user.auth_epoch)


# 从任务快照取得用户和授权代次，交由统一通知逻辑校验并去重。
async def notify_run_reauth(run_id):
    from app.db import Run

    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run:
            user_id, epoch = run.user_id, run.auth_epoch
        else:
            return
    await notify_reauth(user_id, epoch)
