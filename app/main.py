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
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.db import Audit, Identity, ManualRequest, Run, SessionLocal, Token, User, get_db, now
from app.oauth import router
from app.schemas import (
    Envelope,
    HealthView,
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


# 为请求分配追踪编号和安全响应头，并对管理员接口的成功、拒绝与异常访问记录审计。
@app.middleware("http")
async def request_context(request, call_next):
    request.state.request_id = str(uuid4())
    try:
        response = await call_next(request)
    except Exception:
        if request.url.path.startswith("/api/admin/"):
            await run_in_threadpool(record_admin_access, request, False)
        raise
    if request.url.path.startswith("/api/admin/"):
        await run_in_threadpool(record_admin_access, request, response.status_code < 400)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# 统一成功响应结构，附带请求编号供前后端定位同一次调用。
def ok(request, data):
    return {"data": data, "request_id": request.state.request_id}


# 将已分类业务错误转换为统一响应，保留可重试标志及经过筛选的详情。
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


# 只返回校验失败的字段位置与类别，避免 Pydantic 错误包含提交的敏感原值。
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


# 将框架 HTTP 异常转换为稳定错误结构，不向客户端透传内部异常描述。
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


# 隐藏未分类异常细节，客户端通过请求编号关联排查。
@app.exception_handler(Exception)
async def internal_handler(request, error):
    return await problem_handler(
        request, Problem(500, "INTERNAL_ERROR", "服务暂时不可用，请通过请求编号联系管理员")
    )


# 进程存活探针，不依赖数据库可用性。
@app.get("/health/live")
def live():
    return {"status": "ok"}


# 执行轻量数据库查询，确认 API 已具备访问数据库的能力。
@app.get("/health/ready")
def ready(db: DBSession = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


# 返回当前身份、页面能力和 CSRF 令牌；网页登录状态与消息授权状态分别展示。
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


# 记录最近活动时间，不延长固定会话有效期，也不恢复已过期登录。
@app.post("/api/session/activity")
def activity(request: Request, identity=Depends(authenticated), db: DBSession = Depends(get_db)):
    request.state.session.last_seen_at = now()
    db.commit()
    return ok(request, {"active": True})


# 删除当前服务端会话并清除 Cookie；不撤销离线业务授权和每日计划。
@app.post("/api/logout")
def logout(request: Request, identity=Depends(authenticated), db: DBSession = Depends(get_db)):
    db.delete(request.state.session)
    db.commit()
    response = JSONResponse(ok(request, {"logged_out": True}))
    response.delete_cookie(get_settings().cookie_name, path="/")
    return response


# 读取当前试点用户的助理设置，不允许跨用户查询。
@app.get("/api/me/settings", response_model=Envelope[SettingsView])
def get_my_settings(request: Request, identity=Depends(personal), db: DBSession = Depends(get_db)):
    return ok(request, settings_dto(owner(db, identity)))


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


# 加锁更新每日时间和配置版本，取消尚未开始的旧计划任务并重新计算下次执行时间。
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


# 在用户锁内按幂等键复用任务，检查活动任务与五分钟频率限制后创建新任务。
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
    # 先查幂等记录再查频率限制，使同一次按钮请求重试时返回原任务而非创建重复任务。
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


# 处理启用、暂停、恢复和停用；停用同时移除凭证并推进授权代次，使旧任务失效。
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


# 独立保存管理员访问审计，仅记录路由模板、结果及请求编号。
def record_admin_access(request: Request, allowed: bool):
    # Store the route template, never arbitrary paths/query strings or search terms.
    route = request.scope.get("route")
    action = getattr(route, "path", "/api/admin/unmatched")
    with SessionLocal() as db:
        db.add(
            Audit(
                actor_id=getattr(request.state, "admin_actor_id", None),
                action=action,
                allowed=allowed,
                request_id=request.state.request_id,
            )
        )
        db.commit()


# 校验当前身份的管理员权限，同时保留审计所需的操作者标识。
def admin(request: Request, db: DBSession = Depends(get_db)):
    identity = authenticated(request, db)
    request.state.admin_actor_id = identity.id
    if identity.open_id not in get_settings().admins:
        raise Problem(403, "ADMIN_REQUIRED", "无管理员权限")
    return identity


# 向管理员返回运行健康元数据，不暴露消息、模型输出或密钥。
@app.get("/api/admin/health", response_model=Envelope[HealthView])
def admin_health(request: Request, identity=Depends(admin), db: DBSession = Depends(get_db)):
    from app.health import snapshot

    return ok(request, snapshot(db))


# 分页查询当前租户的助理用户配置及最近任务结果，支持名称与状态筛选。
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


# 构造租户隔离的任务查询；普通用户额外限制为本人，管理员可查看同租户。
def run_query(identity, is_admin):
    query = (
        select(Run, Identity)
        .join(User, Run.user_id == User.id)
        .join(Identity, User.identity_id == Identity.id)
        .where(Identity.tenant_key == identity.tenant_key)
    )
    return query if is_admin else query.where(Identity.id == identity.id)


# 校验带时区且不超过三十天的查询区间，按过滤条件分页返回任务元数据。
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


# 生成个人与管理端共用的列表处理器，通过不同依赖注入访问权限。
def runs_endpoint(is_admin):
    # 将已校验的列表参数交给统一查询逻辑，避免两类页面的筛选规则分叉。
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


# 生成个人与管理端共用的详情处理器，复用统一的租户和身份隔离规则。
def detail_endpoint(is_admin):
    # 读取可见任务及发送分段状态；不可见与不存在都返回 404，避免泄露任务存在性。
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


# 提供公开登录入口与预设演示账号，不向匿名调用者公开真实用户白名单。
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
