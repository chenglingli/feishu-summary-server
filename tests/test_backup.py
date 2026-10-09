import os

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import delete, select

from app.backup import decrypt, encrypt, export_config, restore_config
from app.config import get_settings
from app.db import Base, SessionLocal, Token, User


def test_configuration_backup_restore_requires_reauthorization(clients, monkeypatch):
    monkeypatch.setattr(get_settings(), "maintenance_mode", True)
    with SessionLocal() as db:
        archive = export_config(db)
        encoded = archive.model_dump_json()
        assert "csrf_token" not in encoded and "ciphertext" not in encoded
        key = os.urandom(32)
        wire = encrypt(archive, key)
        assert b"demo-a" not in wire
        restored = decrypt(wire, key)
        with pytest.raises(InvalidTag):
            decrypt(wire[:-1] + bytes([wire[-1] ^ 1]), key)
        with pytest.raises(ValueError, match="empty"):
            restore_config(db, restored)
        db.rollback()
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(delete(table))
        restore_config(db, restored)
        db.commit()
        users = list(db.scalars(select(User)))
        assert len(users) == len(archive.users)
        originals = {u.id: u for u in archive.users}
        assert all(u.auth_status == "needs_reauth" and u.next_run_at is None for u in users)
        assert all(u.auth_epoch == originals[u.id].auth_epoch + 1 for u in users)
        assert all(u.enabled_status in ("paused", "disabled") for u in users)
        assert not list(db.scalars(select(Token)))


def test_restore_checks_maintenance_and_schema(monkeypatch):
    with SessionLocal() as db:
        archive = export_config(db)
        with pytest.raises(ValueError, match="maintenance"):
            restore_config(db, archive)
        monkeypatch.setattr(get_settings(), "maintenance_mode", True)
        archive.revision = "wrong-schema"
        with pytest.raises(ValueError, match="migrations"):
            restore_config(db, archive)
