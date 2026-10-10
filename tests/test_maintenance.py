from sqlalchemy import select

from app.config import get_settings
from app.db import AuthNotice, SessionLocal, User
from app.maintenance import maintain_credentials, mark_needs_reauth, notify_reauth


def test_stale_failure_cannot_revoke_new_authorization(clients):
    with SessionLocal() as db:
        user = db.scalar(select(User).limit(1))
        identifier, epoch = user.id, user.auth_epoch
    mark_needs_reauth(identifier, epoch - 1)
    with SessionLocal() as db:
        assert db.get(User, identifier).auth_status == "valid"
    mark_needs_reauth(identifier, epoch)
    with SessionLocal() as db:
        assert db.get(User, identifier).auth_status == "needs_reauth"


async def test_notification_once_per_epoch_even_after_metadata_cleanup(clients, monkeypatch):
    import app.maintenance as module

    cfg = get_settings()
    monkeypatch.setattr(cfg, "mock_auth_enabled", False)
    monkeypatch.setattr(cfg, "live_pipeline_enabled", True)
    calls = []

    async def token():
        return "fake"

    async def send(*args):
        calls.append(args)
        return "message-fake"

    monkeypatch.setattr(module, "bot_token", token)
    monkeypatch.setattr(module, "send_part", send)
    with SessionLocal() as db:
        user = db.scalar(select(User).limit(1))
        user.auth_status, user.enabled_status = "needs_reauth", "enabled"
        db.commit()
    await notify_reauth(user.id, user.auth_epoch)
    with SessionLocal() as db:
        notice = db.get(AuthNotice, (user.id, user.auth_epoch))
        assert notice.status == "delivered"
        db.delete(notice)
        db.commit()
    await notify_reauth(user.id, user.auth_epoch)
    assert len(calls) == 1


async def test_maintenance_skips_paused_users(clients, monkeypatch):
    import app.maintenance as module

    cfg = get_settings()
    monkeypatch.setattr(cfg, "mock_auth_enabled", False)
    monkeypatch.setattr(cfg, "live_pipeline_enabled", True)
    calls = []

    async def refresh(*args):
        calls.append(args)
        return "fake"

    monkeypatch.setattr(module, "user_access", refresh)
    with SessionLocal() as db:
        for user in db.scalars(select(User)):
            user.enabled_status = "paused"
        db.commit()
    await maintain_credentials()
    assert not calls
