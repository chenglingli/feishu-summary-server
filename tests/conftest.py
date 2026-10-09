import os

# A dedicated database is mandatory; never point destructive fixture setup at the application DB.
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("MOCK_AUTH_ENABLED", "true")
os.environ.setdefault("PILOT_OPEN_IDS", "demo-a,demo-b")
os.environ.setdefault("ADMIN_OPEN_IDS", "demo-admin")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:5173")
if not os.environ.get("DATABASE_URL", "").endswith("/summary_test"):
    raise RuntimeError("Tests require an explicit DATABASE_URL ending in /summary_test")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def clean_database():
    with engine.begin() as conn:
        names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {names} CASCADE"))
    yield


@pytest.fixture
def clients():
    result = {}
    for name in ("demo-a", "demo-b", "demo-admin"):
        client = TestClient(app, base_url="http://localhost:5173")
        response = client.get(f"/auth/dev/login/{name}", follow_redirects=False)
        assert response.status_code == 303
        csrf = client.get("/api/session").json()["data"]["csrf_token"]
        client.headers.update({"X-CSRF-Token": csrf, "Origin": "http://localhost:5173"})
        result[name] = client
    yield result
    for client in result.values():
        client.close()


@pytest.fixture(autouse=True)
def payload_directory(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "payload_dir", str(tmp_path / "payloads"))
