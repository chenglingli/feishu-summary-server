from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.db import Identity, Run, Session, SessionLocal, User, now
from app.main import app
from app.runner import execute, recover, schedule
from app.services import next_schedule


def enqueue(client):
    response = client.post("/api/me/runs", headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 200, response.text
    return response.json()["data"]["run_id"]


def test_anonymous_and_csrf(clients):
    anonymous = TestClient(app, base_url="http://localhost:5173")
    assert anonymous.get("/api/me/settings").status_code == 401
    a = clients["demo-a"]
    assert (
        a.patch(
            "/api/me/settings", json={"daily_time": "20:00"}, headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    assert (
        a.patch(
            "/api/me/settings",
            json={"daily_time": "20:00"},
            headers={"Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    assert (
        a.patch("/api/me/settings", json={"daily_time": "20:00", "owner_id": "other"}).status_code
        == 422
    )
    assert a.patch("/api/me/settings", json={"daily_time": "25:00"}).status_code == 422
    assert a.get("/api/admin/users").status_code == 403


def test_isolation_idempotency_admin_metadata(clients):
    a, b, admin = (clients[x] for x in ("demo-a", "demo-b", "demo-admin"))
    key = str(uuid4())
    first = a.post("/api/me/runs", headers={"Idempotency-Key": key})
    assert first.status_code == 200, first.text
    run_id = first.json()["data"]["run_id"]
    assert (
        a.post("/api/me/runs", headers={"Idempotency-Key": key}).json()["data"]["run_id"] == run_id
    )
    assert a.post("/api/me/runs", headers={"Idempotency-Key": str(uuid4())}).status_code == 409
    assert b.get(f"/api/me/runs/{run_id}").status_code == 404
    assert b.get("/api/me/runs").json()["data"]["total"] == 0
    assert admin.get(f"/api/admin/runs/{run_id}").status_code == 200
    users = admin.get("/api/admin/users").json()["data"]
    assert users["total"] == 2  # Pure administrators are identities, not assistant users.
    assert "ciphertext" not in str(users) and "csrf_token" not in str(users)
    assert admin.get("/api/admin/runs?page_size=101").status_code == 422


def test_settings_logout_and_idle_session(clients):
    a = clients["demo-a"]
    assert a.post("/api/me/enable").status_code == 200
    assert (
        a.patch("/api/me/settings", json={"daily_time": "21:30"}).json()["data"]["daily_time"]
        == "21:30"
    )
    with SessionLocal() as db:
        identity = db.scalar(select(Identity).where(Identity.open_id == "demo-a"))
        session = db.scalar(select(Session).where(Session.identity_id == identity.id))
        last_seen = session.last_seen_at
    a.get("/api/me/runs")
    with SessionLocal() as db:
        assert db.get(Session, session.id_hash).last_seen_at == last_seen
    assert a.post("/api/logout").status_code == 200
    assert a.get("/api/session").status_code == 401
    with SessionLocal() as db:
        assert (
            db.scalar(select(User).where(User.identity_id == identity.id)).enabled_status
            == "enabled"
        )


async def test_two_users_complete_demo_without_claiming_delivery(clients):
    ids = [enqueue(clients[name]) for name in ("demo-a", "demo-b")]
    import asyncio

    await asyncio.gather(*(execute(run_id) for run_id in ids))
    for run_id in ids:
        data = clients["demo-admin"].get(f"/api/admin/runs/{run_id}").json()["data"]
        assert data["execution_status"] == "succeeded"
        assert data["delivery_status"] == "not_started"
        assert data["coverage_status"] == "partial"
        assert data["safe_error"] == "DEMO_NOT_SENT"


def test_scheduler_catchup_is_unique_and_records_missed(clients):
    with SessionLocal() as db:
        user = db.scalar(select(User).limit(1))
        user.enabled_status = "enabled"
        user.next_run_at = now() - timedelta(days=3, minutes=30)
        user.daily_time = user.next_run_at.astimezone(
            __import__("zoneinfo").ZoneInfo("Asia/Shanghai")
        ).strftime("%H:%M")
        db.commit()
    schedule()
    schedule()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 4
        assert (
            db.scalar(select(func.count()).select_from(Run).where(Run.execution_status == "missed"))
            == 3
        )


def test_disable_cancels_task_but_preserves_login(clients):
    run_id = enqueue(clients["demo-a"])
    assert clients["demo-a"].post("/api/me/disable").status_code == 200
    assert clients["demo-a"].get("/api/session").status_code == 200
    assert (
        clients["demo-a"].get(f"/api/me/runs/{run_id}").json()["data"]["execution_status"]
        == "cancelled"
    )
    assert (
        clients["demo-a"]
        .post("/api/me/runs", headers={"Idempotency-Key": str(uuid4())})
        .status_code
        == 409
    )


def test_restart_does_not_resend_unknown_delivery(clients):
    from app.db import DeliveryPart

    run_id = enqueue(clients["demo-a"])
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        run.execution_status, run.delivery_status = "sending", "sending"
        db.add(DeliveryPart(run_id=run_id, part=1, uuid=str(uuid4()), payload_hash="fake"))
        db.commit()
    recover()
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run.execution_status == "delivery_unknown"
        assert run.delivery_status == "unknown"


def test_production_rejects_mock():
    import pytest

    with pytest.raises(ValueError):
        Settings(
            app_env="production",
            public_base_url="https://summary.example.com",
            mock_auth_enabled=True,
        )


def test_schedule_strictly_future():
    from datetime import datetime, timezone

    cutoff = datetime(2026, 9, 30, 11, tzinfo=timezone.utc)
    assert next_schedule("19:00", cutoff) == cutoff + timedelta(days=1)


def test_oauth_state_must_be_browser_bound_and_not_expired(clients):
    from app.db import OAuthState
    from app.security import digest

    client = clients["demo-a"]
    with SessionLocal() as db:
        db.add(
            OAuthState(
                id_hash=digest("state"),
                binding_hash=digest("binding"),
                purpose="login",
                expires_at=now() - timedelta(seconds=1),
            )
        )
        db.commit()
    assert client.get("/auth/login/callback?state=state&code=fake").status_code == 400
    assert client.get("/auth/login/callback?state=unknown&code=fake").status_code == 400


def test_token_encryption_is_bound_to_user(tmp_path, monkeypatch):
    import base64
    import os

    import pytest
    from cryptography.exceptions import InvalidTag

    from app.config import get_settings
    from app.security import decrypt_tokens, encrypt_tokens

    key = tmp_path / "key"
    key.write_text(base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setattr(get_settings(), "token_encryption_key_file", str(key))
    sealed = encrypt_tokens("a", {"access_token": "fake-secret"})
    assert "fake-secret" not in sealed
    assert decrypt_tokens("a", sealed)["access_token"] == "fake-secret"
    with pytest.raises(InvalidTag):
        decrypt_tokens("b", sealed)


def test_long_idle_session_and_allowlist_removal(clients, monkeypatch):
    from app.config import get_settings

    with SessionLocal() as db:
        identity = db.scalar(select(Identity).where(Identity.open_id == "demo-a"))
        session = db.scalar(select(Session).where(Session.identity_id == identity.id))
        session.last_seen_at = now() - timedelta(days=30)
        db.commit()
    assert clients["demo-a"].get("/api/session").status_code == 200
    monkeypatch.setattr(get_settings(), "pilot_open_ids", "demo-a")
    assert clients["demo-b"].get("/api/session").status_code == 403


def test_oauth_state_consumed_and_identity_login_does_not_grant_messages(clients, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    import httpx

    import app.oauth as oauth

    monkeypatch.setattr(Settings, "secret", lambda self, _: "fake-secret")
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "mock_auth_enabled", False)
    monkeypatch.setattr(get_settings(), "feishu_app_id", "fake-app")
    monkeypatch.setattr(get_settings(), "feishu_app_secret_file", "fake-path")
    original_client = httpx.AsyncClient

    def response(request):
        if "/token" in request.url.path:
            return httpx.Response(
                200, json={"access_token": "temporary-token", "refresh_token": "must-discard"}
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"tenant_key": "local-demo", "open_id": "demo-admin", "name": "管理员"},
            },
        )

    monkeypatch.setattr(
        oauth.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(response), **kwargs),
    )
    client = clients["demo-admin"]
    start = client.get("/auth/login/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback_url = f"/auth/login/callback?state={state}&code=fake-code"
    result = client.get(callback_url, follow_redirects=False)
    assert result.status_code == 303, result.text
    assert client.get(callback_url, follow_redirects=False).status_code == 400
    with SessionLocal() as db:
        identity = db.scalar(select(Identity).where(Identity.open_id == "demo-admin"))
        assert db.scalar(select(User).where(User.identity_id == identity.id)) is None


def test_unverified_live_mode_cannot_enable_schedule(clients, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "mock_auth_enabled", False)
    response = clients["demo-a"].post("/api/me/enable")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "POC_REQUIRED"


def test_public_demo_options_and_missing_oauth_config(clients, monkeypatch):
    from app.config import get_settings

    client = TestClient(app, base_url="http://localhost:5173")
    options = client.get("/api/public/login-options").json()["data"]
    assert options["demo"] and len(options["accounts"]) == 3
    assert not options["feishu_enabled"]
    response = client.get("/auth/login/start", follow_redirects=False)
    assert response.status_code == 409 and "location" not in response.headers
    monkeypatch.setattr(get_settings(), "mock_auth_enabled", False)
    assert client.get("/api/public/login-options").json()["data"]["accounts"] == []


def test_denied_oauth_diagnostics_preserve_access_control(clients, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    import httpx

    import app.oauth as oauth
    from app.config import get_settings

    cfg = get_settings()
    monkeypatch.setattr(cfg, "mock_auth_enabled", False)
    monkeypatch.setattr(cfg, "feishu_app_id", "fake-app")
    monkeypatch.setattr(cfg, "feishu_app_secret_file", "fake-path")
    monkeypatch.setattr(Settings, "secret", lambda self, _: "fake-secret")
    original = httpx.AsyncClient
    info = {"tenant_key": "local-demo", "open_id": "outside-user"}

    def respond(request):
        if "/token" in request.url.path:
            return httpx.Response(200, json={"access_token": "fake-access"})
        return httpx.Response(200, json={"code": 0, "data": info})

    monkeypatch.setattr(
        oauth.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    client = clients["demo-admin"]
    for environment, tenant, reason in [
        ("development", "local-demo", "USER_NOT_ALLOWED"),
        ("development", "other-tenant", "TENANT_MISMATCH"),
        ("production", "other-tenant", None),
    ]:
        monkeypatch.setattr(cfg, "app_env", environment)
        info["tenant_key"] = tenant
        response = client.get("/auth/login/start", follow_redirects=False)
        state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
        result = client.get(
            f"/auth/login/callback?state={state}&code=fake-code", follow_redirects=False
        )
        assert result.status_code == 403
        details = result.json()["error"]["details"]
        if reason:
            assert details["reason"] == reason
            assert details["login_identity"] == info
        else:
            assert details is None
        assert "fake-access" not in result.text and "fake-secret" not in result.text
    with SessionLocal() as db:
        assert db.scalar(select(Identity).where(Identity.open_id == "outside-user")) is None


def test_absolute_session_expiry_still_requires_login(clients):
    with SessionLocal() as db:
        identity = db.scalar(select(Identity).where(Identity.open_id == "demo-a"))
        session = db.scalar(select(Session).where(Session.identity_id == identity.id))
        session.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert clients["demo-a"].get("/api/session").status_code == 401


def test_persistent_cookie_matches_configured_database_expiry(clients, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "session_lifetime_days", 120)
    before = now()
    response = clients["demo-a"].get("/auth/dev/login/demo-a", follow_redirects=False)
    assert "Max-Age=10368000" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    with SessionLocal() as db:
        identity = db.scalar(select(Identity).where(Identity.open_id == "demo-a"))
        session = db.scalar(
            select(Session)
            .where(Session.identity_id == identity.id)
            .order_by(Session.expires_at.desc())
        )
        assert before + timedelta(days=120) <= session.expires_at <= now() + timedelta(days=120)
