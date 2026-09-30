from datetime import timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DBSession

from app.config import get_settings
from app.db import Identity, OAuthState, Session, Token, User, get_db, now
from app.security import Problem, authenticated, digest, encrypt_tokens, personal, random_token

router = APIRouter()
SCOPES = {
    "offline_access",
    "search:message",
    "im:message:readonly",
    "im:message.p2p_msg:get_as_user",
    "im:message.group_msg:get_as_user",
}


def new_session(db, identity, response, old_hash=None):
    if old_hash:
        db.execute(delete(Session).where(Session.id_hash == old_hash))
    raw = random_token()
    db.add(
        Session(
            id_hash=digest(raw),
            identity_id=identity.id,
            csrf_token=random_token(),
            expires_at=now() + timedelta(hours=8),
        )
    )
    cfg = get_settings()
    response.set_cookie(
        cfg.cookie_name,
        raw,
        secure=cfg.public_base_url.startswith("https://"),
        httponly=True,
        samesite="lax",
        max_age=28800,
        path="/",
    )


def start(request, db, purpose, identity=None):
    cfg = get_settings()
    state, binding = random_token(), random_token()
    old_hash = digest(request.cookies.get(cfg.cookie_name, ""))
    db.add(
        OAuthState(
            id_hash=digest(state),
            binding_hash=digest(binding),
            purpose=purpose,
            session_hash=old_hash if identity else None,
            identity_id=identity.id if identity else None,
            expires_at=now() + timedelta(minutes=5),
        )
    )
    url = "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urlencode(
        {
            "client_id": cfg.feishu_app_id,
            "redirect_uri": f"{cfg.public_base_url}/auth/{purpose}/callback",
            "response_type": "code",
            "scope": " ".join(sorted(SCOPES)) if identity else "",
            "state": state,
        }
    )
    response = (
        RedirectResponse(url)
        if purpose == "login"
        else JSONResponse(
            {"data": {"authorization_url": url}, "request_id": request.state.request_id}
        )
    )
    response.set_cookie(
        f"oauth-{purpose}",
        binding,
        max_age=300,
        httponly=True,
        secure=cfg.public_base_url.startswith("https://"),
        samesite="lax",
        path="/",
    )
    db.commit()
    return response


@router.get("/auth/login/start")
def login_start(request: Request, db: DBSession = Depends(get_db)):
    return start(request, db, "login")


@router.post("/auth/feishu/start")
def feishu_start(request: Request, identity=Depends(personal), db: DBSession = Depends(get_db)):
    return start(request, db, "feishu", identity)


@router.get("/auth/{purpose}/callback")
async def callback(
    purpose: str, request: Request, state: str = "", code: str = "", db: DBSession = Depends(get_db)
):
    cfg = get_settings()
    row = db.scalar(select(OAuthState).where(OAuthState.id_hash == digest(state)).with_for_update())
    if (
        not row
        or row.purpose != purpose
        or row.expires_at <= now()
        or row.binding_hash != digest(request.cookies.get(f"oauth-{purpose}", ""))
    ):
        raise Problem(400, "OAUTH_STATE_INVALID", "授权请求已过期或不属于当前浏览器")
    expected_id, expected_session = row.identity_id, row.session_hash
    if purpose == "feishu":
        identity = authenticated(request, db)
        if identity.id != expected_id or request.state.session.id_hash != expected_session:
            raise Problem(403, "OAUTH_IDENTITY_MISMATCH", "请使用发起授权的账号")
    db.delete(row)
    db.commit()  # Consume before network I/O; a failed code exchange must start a new transaction.
    if not code:
        raise Problem(400, "OAUTH_CANCELLED", "授权未完成，请重新发起")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(
                "https://accounts.feishu.cn/oauth/v3/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": cfg.feishu_app_id,
                    "client_secret": cfg.secret("feishu_app_secret_file"),
                    "code": code,
                    "redirect_uri": f"{cfg.public_base_url}/auth/{purpose}/callback",
                },
            )
            res.raise_for_status()
            token = res.json()
            access = token["access_token"]
            info = await client.get(
                "https://open.feishu.cn/open-apis/authen/v1/user_info",
                headers={"Authorization": f"Bearer {access}"},
            )
            info.raise_for_status()
            payload = info.json()
            if payload.get("code") != 0:
                raise ValueError("userinfo failed")
            user_info = payload["data"]
    except (httpx.HTTPError, ValueError, KeyError):
        raise Problem(502, "OAUTH_EXCHANGE_FAILED", "飞书授权失败，请重新授权") from None
    if (
        user_info.get("tenant_key") != cfg.allowed_tenant_key
        or user_info.get("open_id") not in cfg.pilots | cfg.admins
    ):
        raise Problem(403, "ACCESS_DENIED", "此飞书账号不在应用可用范围")
    identity = db.scalar(
        select(Identity).where(
            Identity.tenant_key == cfg.allowed_tenant_key, Identity.open_id == user_info["open_id"]
        )
    )
    if not identity:
        identity = Identity(
            tenant_key=cfg.allowed_tenant_key,
            open_id=user_info["open_id"],
            name=user_info.get("name", ""),
        )
        db.add(identity)
        db.flush()
    if identity.access_status != "active" or (expected_id and identity.id != expected_id):
        raise Problem(403, "OAUTH_IDENTITY_MISMATCH", "授权账号不匹配或已停用")
    if purpose == "feishu":
        if (
            identity.open_id not in cfg.pilots
            or not SCOPES <= set(token.get("scope", "").split())
            or not token.get("refresh_token")
        ):
            raise Problem(409, "AUTHORIZATION_REQUIRED", "消息或离线权限不完整，请联系管理员")
        from app.services import cancel_runs, next_schedule

        user = db.scalar(select(User).where(User.identity_id == identity.id).with_for_update())
        if not user:
            user = User(identity_id=identity.id)
            db.add(user)
            db.flush()
        else:
            cancel_runs(db, user)
            user.auth_epoch += 1
        user.auth_status, user.last_authorized_at, user.auth_checked_at = "valid", now(), now()
        if user.enabled_status == "enabled":
            user.next_run_at = next_schedule(user.daily_time)
        lifetime = int(token.get("refresh_token_expires_in", 0))
        if lifetime <= 0 or int(token.get("expires_in", 0)) <= 0:
            raise Problem(502, "TOKEN_EXPIRY_INVALID", "授权返回缺少凭证有效期")
        saved = db.get(Token, user.id)
        if not saved:
            saved = Token(user_id=user.id, version=0)
            db.add(saved)
        saved.ciphertext = encrypt_tokens(user.id, token)
        saved.version += 1
        saved.expires_at = now() + timedelta(seconds=int(token["expires_in"]))
        saved.refresh_expires_at = now() + timedelta(seconds=lifetime)
        saved.refresh_after = saved.refresh_expires_at - min(
            timedelta(hours=24), timedelta(seconds=lifetime * 0.2)
        )
    # Identity-only login deliberately discards OAuth tokens instead of overwriting offline grants.
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(f"oauth-{purpose}", path="/")
    new_session(db, identity, response, digest(request.cookies.get(cfg.cookie_name, "")))
    db.commit()
    return response


@router.get("/auth/dev/login/{open_id}", include_in_schema=False)
def dev_login(open_id: str, db: DBSession = Depends(get_db)):
    cfg = get_settings()
    if not cfg.mock_auth_enabled or open_id not in cfg.pilots | cfg.admins:
        raise Problem(404, "NOT_FOUND", "页面不存在")
    identity = db.scalar(
        select(Identity).where(
            Identity.tenant_key == cfg.allowed_tenant_key, Identity.open_id == open_id
        )
    )
    if not identity:
        identity = Identity(
            tenant_key=cfg.allowed_tenant_key, open_id=open_id, name=f"演示用户 {open_id}"
        )
        db.add(identity)
        db.flush()
    if open_id in cfg.pilots and not db.scalar(select(User).where(User.identity_id == identity.id)):
        db.add(User(identity_id=identity.id))
    response = RedirectResponse("/", status_code=303)
    new_session(db, identity, response)
    db.commit()
    return response
