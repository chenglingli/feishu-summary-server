import base64
import hashlib
import json
import secrets
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, Request
from sqlalchemy.orm import Session as DBSession

from app.config import get_settings
from app.db import Identity, Session, get_db, now


class Problem(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def random_token() -> str:
    return secrets.token_urlsafe(32)


def encrypt_tokens(user_id: str, payload: dict) -> str:
    key = base64.b64decode(get_settings().secret("token_encryption_key_file"), validate=True)
    if len(key) != 32:
        raise ValueError("Encryption key must be 32 bytes, base64 encoded")
    nonce = secrets.token_bytes(12)
    # AAD prevents transplanting credentials between users or purposes.
    data = AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), f"{user_id}:tokens:v1".encode())
    return "v1:" + base64.b64encode(nonce + data).decode()


def decrypt_tokens(user_id: str, ciphertext: str) -> dict:
    version, encoded = ciphertext.split(":", 1)
    if version != "v1":
        raise ValueError("Unknown encryption key version")
    data = base64.b64decode(encoded)
    key = base64.b64decode(get_settings().secret("token_encryption_key_file"), validate=True)
    return json.loads(AESGCM(key).decrypt(data[:12], data[12:], f"{user_id}:tokens:v1".encode()))


def authenticated(request: Request, db: DBSession = Depends(get_db)):
    cfg = get_settings()
    session = db.get(Session, digest(request.cookies.get(cfg.cookie_name, "")))
    # Persistent login expires at a fixed deadline rather than after inactivity.
    # Server-side logout and current access restrictions are still checked on every request.
    if not session or session.expires_at <= now():
        raise Problem(401, "UNAUTHENTICATED", "请重新登录")
    identity = db.get(Identity, session.identity_id)
    if (
        not identity
        or identity.access_status != "active"
        or identity.tenant_key != cfg.allowed_tenant_key
        or identity.open_id not in cfg.pilots | cfg.admins
    ):
        raise Problem(403, "ACCESS_DENIED", "当前账号不在应用可用范围")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin is None:
            ref = urlparse(request.headers.get("referer", ""))
            origin = f"{ref.scheme}://{ref.netloc}"
        if origin != cfg.public_base_url or not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), session.csrf_token
        ):
            raise Problem(403, "CSRF_REJECTED", "请求校验失败，请刷新页面重试")
        if cfg.maintenance_mode:
            raise Problem(503, "MAINTENANCE", "服务维护中，请稍后重试")
    request.state.identity = identity
    request.state.session = session
    return identity


def personal(identity: Identity = Depends(authenticated)):
    if identity.open_id not in get_settings().pilots:
        raise Problem(403, "PILOT_REQUIRED", "此账号尚未加入试点")
    return identity
