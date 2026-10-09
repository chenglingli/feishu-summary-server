from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session as DBSession
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.db import Audit, Identity, ManualRequest, Run, Token, User, get_db, now
from app.oauth import router
from app.schemas import (
    Envelope,
    LoginOptions,
    Page,
    RunCreated,
    RunDetail,
    RunView,
    SessionInfo,
    SettingsView,
    UserView,
)
from app.security import Problem, authenticated, digest, personal
from app.services import (
    ACTIVE,
    cancel_runs,
    create_run,
    next_schedule,
    owner,
    require_authorized,
    run_dto,
    settings_dto,
)

app = FastAPI(title="Feishu Summary API", version="0.1.0", docs_url=None, redoc_url=None)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[
        urlparse(get_settings().public_base_url).hostname or "localhost",
        "api",
        "testserver",
        "127.0.0.1",
    ],
)
app.include_router(router)


@app.middleware("http")
async def request_context(request, call_next):
    request.state.request_id = str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def ok(request, data):
    return {"data": data, "request_id": request.state.request_id}


@app.exception_handler(Problem)
async def problem_handler(request, error):
    return JSONResponse(
        status_code=error.status,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": error.status in (429, 502, 503),
                "details": error.details,
            },
            "request_id": request.state.request_id,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request, error):
    # Pydantic errors may contain submitted secrets; only expose field locations and error categories.
    return await problem_handler(
        request,
        Problem(
            422,
            "VALIDATION_ERROR",
            "请求参数不正确",
            [{"field": list(x["loc"]), "type": x["type"]} for x in error.errors()],
        ),
    )


@app.exception_handler(HTTPException)
async def http_handler(request, error):
    return await problem_handler(
        request,
        Problem(
            error.status_code,
            "NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR",
            "请求无法处理",
        ),
    )


@app.exception_handler(Exception)
async def internal_handler(request, error):
    return await problem_handler(
        request, Problem(500, "INTERNAL_ERROR", "服务暂时不可用，请通过请求编号联系管理员")
    )


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready(db: DBSession = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/session", response_model=Envelope[SessionInfo])
def session_info(
    request: Request, identity=Depends(authenticated), db: DBSession = Depends(get_db)
):
    cfg = get_settings()
    return ok(
        request,
        {
            "identity": {"name": identity.name, "open_id": identity.open_id},
            "capabilities": {
                "personal": identity.open_id in cfg.pilots,
                "admin": identity.open_id in cfg.admins,
            },
            "authorization": settings_dto(owner(db, identity))["authorization"],
            "csrf_token": request.state.session.csrf_token,
            "demo": cfg.mock_auth_enabled,
        },
    )


@app.post("/api/session/activity")
def activity(request: Request, identity=Depends(authenticated), db: DBSession = Depends(get_db)):
    request.state.session.last_seen_at = now()
    db.commit()
    return ok(request, {"active": True})


@app.post("/api/logout")
def logout(request: Request, identity=Depends(authenticated), db: DBSession = Depends(get_db)):
    db.delete(request.state.session)
    db.commit()
    response = JSONResponse(ok(request, {"logged_out": True}))
    response.delete_cookie(get_settings().cookie_name, path="/")
    return response


@app.get("/api/me/settings", response_model=Envelope[SettingsView])
def get_my_settings(request: Request, identity=Depends(personal), db: DBSession = Depends(get_db)):
    return ok(request, settings_dto(owner(db, identity)))


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


@app.patch("/api/me/settings", response_model=Envelope[SettingsView])
def update_settings(
    payload: SettingsInput,
    request: Request,
    identity=Depends(personal),
    db: DBSession = Depends(get_db),
):
    user = owner(db, identity, lock=True)
    if not user:
        raise Problem(409, "AUTHORIZATION_REQUIRED", "请先授权再保存设置")
    user.daily_time = payload.daily_time
    user.config_version += 1
    if user.enabled_status == "enabled":
        user.next_run_at = next_schedule(user.daily_time)
    for run in db.scalars(
        select(Run).where(Run.user_id == user.id, Run.execution_status == "queued")
    ):
        run.execution_status, run.finished_at = "cancelled", now()
    db.commit()
    return ok(request, settings_dto(user))


@app.post("/api/me/runs", response_model=Envelope[RunCreated])
def manual_run(
    request: Request,
    idempotency_key: str = Header(min_length=16, max_length=128),
    identity=Depends(personal),
    db: DBSession = Depends(get_db),
):
    user = owner(db, identity, lock=True)
    require_authorized(user)
    assert user is not None
    existing = db.scalar(
        select(ManualRequest).where(
            ManualRequest.user_id == user.id,
            ManualRequest.key_hash == digest(idempotency_key),
            ManualRequest.expires_at > now(),
        )
    )
    if existing:
        return ok(request, {"run_id": existing.run_id})
    active = db.scalar(select(Run).where(Run.user_id == user.id, Run.execution_status.in_(ACTIVE)))
    if active:
        raise Problem(409, "RUN_ALREADY_ACTIVE", "已有任务执行中", {"existing_run_id": active.id})
    if db.scalar(
        select(Run.id).where(
            Run.user_id == user.id,
            Run.kind == "manual",
            Run.scheduled_at > now() - timedelta(minutes=5),
        )
    ):
        raise Problem(429, "RUN_RATE_LIMITED", "手动任务间隔至少五分钟")
    if not get_settings().mock_auth_enabled and not get_settings().live_pipeline_enabled:
        raise Problem(409, "POC_REQUIRED", "真实消息处理尚未通过租户联调，暂不开放执行")
    run = create_run(user, "manual", now())
    db.add(run)
    db.flush()
    db.add(
        ManualRequest(
            user_id=user.id,
            key_hash=digest(idempotency_key),
            run_id=run.id,
            expires_at=now() + timedelta(hours=24),
        )
    )
    db.commit()
    return ok(request, {"run_id": run.id})


@app.post("/api/me/{action}", response_model=Envelope[SettingsView])
def transition(
    action: Literal["enable", "pause", "resume", "disable"],
    request: Request,
    identity=Depends(personal),
    db: DBSession = Depends(get_db),
):
    user = owner(db, identity, lock=True)
    if not user:
        raise Problem(409, "AUTHORIZATION_REQUIRED", "请先授权")
    if action in ("enable", "resume"):
        cfg = get_settings()
        if not cfg.mock_auth_enabled and not cfg.live_pipeline_enabled:
            raise Problem(409, "POC_REQUIRED", "真实消息处理尚未通过租户联调，暂不开放定时执行")
        # A disabled user can re-enable after obtaining a new business grant.
        if user.auth_status != "valid":
            raise Problem(409, "AUTHORIZATION_REQUIRED", "请重新授权")
        user.enabled_status, user.next_run_at = "enabled", next_schedule(user.daily_time)
    else:
        cancel_runs(db, user)
        user.enabled_status = "paused" if action == "pause" else "disabled"
        user.next_run_at = None
        if action == "disable":
            user.auth_epoch += 1
            user.auth_status = "removed"
            db.execute(delete(Token).where(Token.user_id == user.id))
    user.config_version += 1
    db.commit()
    return ok(request, settings_dto(user))


def admin(request: Request, db: DBSession = Depends(get_db)):
    identity = None
    try:
        identity = authenticated(request, db)
        if identity.open_id not in get_settings().admins:
            raise Problem(403, "ADMIN_REQUIRED", "无管理员权限")
    except Problem:
        db.add(
            Audit(
                actor_id=identity.id if identity else None,
                action=request.url.path,
                allowed=False,
                request_id=request.state.request_id,
            )
        )
        db.commit()
        raise
    db.add(
        Audit(
            actor_id=identity.id,
            action=request.url.path,
            allowed=True,
            request_id=request.state.request_id,
        )
    )
    db.commit()
    return identity


@app.get("/api/admin/users", response_model=Envelope[Page[UserView]])
def users(
    request: Request,
    q: str = Query("", max_length=100),
    auth_status: Literal["valid", "needs_reauth", "removed"] | None = None,
    enabled_status: Literal["not_enabled", "enabled", "paused", "disabled"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    identity=Depends(admin),
    db: DBSession = Depends(get_db),
):
    query = (
        select(User, Identity)
        .join(Identity, User.identity_id == Identity.id)
        .where(Identity.tenant_key == identity.tenant_key)
    )
    if q:
        query = query.where(
            Identity.name.contains(q, autoescape=True)
            | Identity.open_id.contains(q, autoescape=True)
        )
    if auth_status:
        query = query.where(User.auth_status == auth_status)
    if enabled_status:
        query = query.where(User.enabled_status == enabled_status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(
        query.order_by(User.first_authorized_at.desc(), User.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = []
    for user, person in rows:
        latest = db.scalar(
            select(Run)
            .where(Run.user_id == user.id)
            .order_by(Run.scheduled_at.desc(), Run.id.desc())
            .limit(1)
        )
        items.append(
            {
                "user_id": user.id,
                "name": person.name,
                "open_id": person.open_id,
                **settings_dto(user),
                "auth_status": user.auth_status,
                "first_authorized_at": user.first_authorized_at,
                "last_authorized_at": user.last_authorized_at,
                "auth_checked_at": user.auth_checked_at,
                "latest_run_result": latest.execution_status if latest else None,
            }
        )
    return ok(request, {"items": items, "total": total, "page": page, "page_size": page_size})


def run_query(identity, is_admin):
    query = (
        select(Run, Identity)
        .join(User, Run.user_id == User.id)
        .join(Identity, User.identity_id == Identity.id)
        .where(Identity.tenant_key == identity.tenant_key)
    )
    return query if is_admin else query.where(Identity.id == identity.id)


def list_runs(
    request,
    identity,
    db,
    page,
    page_size,
    since,
    until,
    execution_status,
    user_id,
    kind,
    coverage_status,
    delivery_status,
):
    end, start = until or now(), since or now() - timedelta(days=7)
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or end <= start
        or end - start > timedelta(days=30)
    ):
        raise Problem(422, "INVALID_TIME_RANGE", "查询区间需带时区，且不超过 30 天")
    query = run_query(identity, request.url.path.startswith("/api/admin/"))
    query = query.where(Run.scheduled_at >= start, Run.scheduled_at < end)
    for column, value in (
        (Run.execution_status, execution_status),
        (Run.user_id, user_id),
        (Run.kind, kind),
        (Run.coverage_status, coverage_status),
        (Run.delivery_status, delivery_status),
    ):
        if value:
            query = query.where(column == value)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(
        query.order_by(Run.scheduled_at.desc(), Run.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return ok(
        request,
        {
            "items": [run_dto(run, person) for run, person in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


def runs_endpoint(is_admin):
    def endpoint(
        request: Request,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        since: datetime | None = Query(None, alias="from"),
        until: datetime | None = Query(None, alias="to"),
        execution_status: Literal[
            "queued",
            "collecting",
            "summarizing",
            "ready_to_send",
            "sending",
            "succeeded",
            "retry_wait",
            "failed",
            "cancelled",
            "missed",
            "needs_reauth",
            "delivery_unknown",
        ]
        | None = None,
        user_id: str | None = None,
        kind: Literal["manual", "scheduled"] | None = None,
        coverage_status: Literal["unknown", "complete", "partial", "no_data"] | None = None,
        delivery_status: Literal[
            "not_started", "sending", "delivered", "partial", "failed", "unknown"
        ]
        | None = None,
        identity=Depends(admin if is_admin else personal),
        db: DBSession = Depends(get_db),
    ):
        return list_runs(
            request,
            identity,
            db,
            page,
            page_size,
            since,
            until,
            execution_status,
            user_id,
            kind,
            coverage_status,
            delivery_status,
        )

    return endpoint


app.get("/api/me/runs", operation_id="my_runs", response_model=Envelope[Page[RunView]])(
    runs_endpoint(False)
)
app.get("/api/admin/runs", operation_id="admin_runs", response_model=Envelope[Page[RunView]])(
    runs_endpoint(True)
)


def detail_endpoint(is_admin):
    def endpoint(
        run_id: str,
        request: Request,
        identity=Depends(admin if is_admin else personal),
        db: DBSession = Depends(get_db),
    ):
        row = db.execute(run_query(identity, is_admin).where(Run.id == run_id)).first()
        if not row:
            raise Problem(404, "NOT_FOUND", "任务不存在")
        data = run_dto(row[0], row[1], detail=True)
        from app.db import DeliveryPart

        data["delivery_parts"] = [
            {"part": part.part, "status": part.status, "message_id": part.message_id}
            for part in db.scalars(
                select(DeliveryPart)
                .where(DeliveryPart.run_id == run_id)
                .order_by(DeliveryPart.part)
            )
        ]
        return ok(request, data)

    return endpoint


app.get("/api/me/runs/{run_id}", operation_id="my_run_detail", response_model=Envelope[RunDetail])(
    detail_endpoint(False)
)
app.get(
    "/api/admin/runs/{run_id}", operation_id="admin_run_detail", response_model=Envelope[RunDetail]
)(detail_endpoint(True))


@app.get("/api/public/login-options", response_model=Envelope[LoginOptions])
def login_options(request: Request):
    cfg = get_settings()
    # Only predefined fake identities are advertised; never expose production allowlists.
    accounts = [
        {"label": label, "login_url": f"/auth/dev/login/{identifier}"}
        for identifier, label in (
            ("demo-a", "员工 A 演示"),
            ("demo-b", "员工 B 演示"),
            ("demo-admin", "管理员演示"),
        )
        if cfg.mock_auth_enabled and identifier in cfg.pilots | cfg.admins
    ]
    return ok(
        request,
        {
            "demo": cfg.mock_auth_enabled,
            "feishu_enabled": bool(
                not cfg.mock_auth_enabled and cfg.feishu_app_id and cfg.feishu_app_secret_file
            ),
            "accounts": accounts,
        },
    )
