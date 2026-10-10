"""Recoverable payloads live only in runner tmpfs, never in PostgreSQL or logs."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from app.config import get_settings
from app.db import now
from app.pipeline import PipelineError


def root():
    path = Path(get_settings().payload_dir)
    if get_settings().app_env == "production":
        # Fail closed if deployment accidentally replaces the content tmpfs with disk storage.
        mounts = [row.split() for row in Path("/proc/mounts").read_text().splitlines()]
        if not any(row[1] == str(path) and row[2] == "tmpfs" for row in mounts):
            raise PipelineError("PAYLOAD_TMPFS_REQUIRED")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def file_path(run_id):
    return root() / (str(UUID(run_id)) + ".json")


def save(run, parts):
    path = file_path(run.id)
    temp = path.with_suffix(".tmp")
    content = json.dumps(
        {
            "user_id": run.user_id,
            "auth_epoch": run.auth_epoch,
            "expires_at": (now() + timedelta(hours=24)).isoformat(),
            "parts": parts,
        },
        ensure_ascii=False,
    )
    descriptor = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def load(run):
    try:
        payload = json.loads(file_path(run.id).read_text())
        if (
            payload["user_id"] != run.user_id
            or payload["auth_epoch"] != run.auth_epoch
            or datetime.fromisoformat(payload["expires_at"]) <= now()
        ):
            raise ValueError("Invalid payload binding")
        parts = payload["parts"]
        if not isinstance(parts, list) or not parts or not all(isinstance(x, str) for x in parts):
            raise ValueError("Invalid payload parts")
        return parts
    except (OSError, ValueError, KeyError, TypeError):
        raise PipelineError("PAYLOAD_LOST") from None


def remove(run_id):
    file_path(run_id).unlink(missing_ok=True)


def clean_expired():
    for path in root().glob("*.json"):
        try:
            expired = datetime.fromisoformat(json.loads(path.read_text())["expires_at"]) <= now()
        except (OSError, ValueError, KeyError, TypeError):
            expired = True
        if expired:
            path.unlink(missing_ok=True)
    for path in root().glob("*.tmp"):
        if now().timestamp() - path.stat().st_mtime > 86400:
            path.unlink(missing_ok=True)
