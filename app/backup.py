"""Encrypted configuration-only backup. Never include tokens, sessions or message data."""

import argparse
import base64
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from app.config import get_settings
from app.db import Base, Identity, SessionLocal, User, now

MAGIC = b"FS-CONFIG-1\n"


class IdentityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    tenant_key: str
    open_id: str
    name: str
    access_status: str


class UserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    identity_id: str
    daily_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    auth_epoch: int = Field(ge=1)
    config_version: int = Field(ge=1)
    first_authorized_at: datetime
    last_authorized_at: datetime
    enabled_status: Literal["enabled", "paused", "not_enabled", "disabled"]


class Archive(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    created_at: datetime
    identities: list[IdentityConfig]
    users: list[UserConfig]


# 按显式字段白名单导出配置，不包含业务凭证、会话和消息数据。
def export_config(db) -> Archive:
    # Explicit field allowlists remain safe even when database models gain secret columns.
    return Archive(
        revision=db.scalar(text("SELECT version_num FROM alembic_version")),
        created_at=now(),
        identities=[
            IdentityConfig(**{k: getattr(row, k) for k in IdentityConfig.model_fields})
            for row in db.scalars(select(Identity))
        ],
        users=[
            UserConfig(**{k: getattr(row, k) for k in UserConfig.model_fields})
            for row in db.scalars(select(User))
        ],
    )


# 在维护模式下锁定并校验空库与迁移版本后恢复配置；恢复用户需重新授权。
def restore_config(db, archive: Archive):
    if not get_settings().maintenance_mode:
        raise ValueError("Restore requires maintenance mode and stopped services")
    # Lock every application table until commit so concurrent writers cannot race the empty check.
    names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    db.execute(text(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE"))
    revision = db.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != archive.revision:
        raise ValueError("Backup and database migrations must match")
    for table in Base.metadata.sorted_tables:
        if db.execute(select(table).limit(1)).first():
            raise ValueError("Restore requires an empty application database")
    for item in archive.identities:
        db.add(Identity(**item.model_dump()))
    db.flush()
    for user in archive.users:
        values = user.model_dump()
        values["enabled_status"] = "disabled" if user.enabled_status == "disabled" else "paused"
        values["auth_epoch"] += 1
        values["config_version"] += 1
        db.add(User(**values, auth_status="needs_reauth", next_run_at=None))
    db.flush()


# 使用独立备份密钥加密归档，并将格式标识作为认证附加数据。
def encrypt(archive: Archive, key: bytes) -> bytes:
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, archive.model_dump_json().encode(), MAGIC)


# 验证备份格式和密文完整性，再按严格归档模型解析解密结果。
def decrypt(data: bytes, key: bytes) -> Archive:
    if not data.startswith(MAGIC):
        raise ValueError("Invalid backup format")
    offset = len(MAGIC)
    plaintext = AESGCM(key).decrypt(data[offset : offset + 12], data[offset + 12 :], MAGIC)
    return Archive.model_validate_json(plaintext)


# 执行加密配置备份或恢复；备份采用一致性只读快照，恢复由事务整体提交。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "restore"])
    parser.add_argument(
        "--key-file", required=True, help="Separate Base64-encoded 32-byte backup key"
    )
    parser.add_argument("--path", required=True, help="Backup directory, or restore archive file")
    args = parser.parse_args()
    try:
        key = base64.b64decode(Path(args.key_file).read_text().strip(), validate=True)
        if len(key) != 32:
            raise ValueError("Backup key must contain 32 bytes")
        target = Path(args.path)
        if args.action == "backup":
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            with SessionLocal() as db:
                db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                data = encrypt(export_config(db), key)
            path = target / f"config-{now().strftime('%Y%m%dT%H%M%S%fZ')}.enc"
            with path.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            # Retention only touches this tool's regular encrypted archives in the chosen directory.
            for old in target.glob("config-*.enc"):
                if (
                    not old.is_symlink()
                    and old.is_file()
                    and old.stat().st_mtime < (now() - timedelta(days=7)).timestamp()
                ):
                    old.unlink()
        else:
            archive = decrypt(target.read_bytes(), key)
            with SessionLocal.begin() as db:
                restore_config(db, archive)
        print(json.dumps({"status": "ok", "action": args.action}))
        return 0
    except Exception:
        # SQL/validation exceptions can contain restored identity data; do not print them.
        print(json.dumps({"status": "failed", "action": args.action}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
