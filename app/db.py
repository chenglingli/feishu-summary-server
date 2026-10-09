from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import get_settings


def now() -> datetime:
    return datetime.now(UTC)


def uid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Identity(Base):
    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("tenant_key", "open_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_key: Mapped[str] = mapped_column(String(128))
    open_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(200))
    access_status: Mapped[str] = mapped_column(default="active")


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), unique=True)
    auth_status: Mapped[str] = mapped_column(default="valid")
    auth_epoch: Mapped[int] = mapped_column(default=1)
    reauth_notice_epoch: Mapped[int] = mapped_column(default=0, server_default="0")
    first_authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    auth_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    enabled_status: Mapped[str] = mapped_column(default="not_enabled")
    daily_time: Mapped[str] = mapped_column(default="19:00")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_version: Mapped[int] = mapped_column(default=1)


class Token(Base):
    __tablename__ = "user_tokens"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    ciphertext: Mapped[str]
    version: Mapped[int] = mapped_column(default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Session(Base):
    __tablename__ = "sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    csrf_token: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OAuthState(Base):
    __tablename__ = "oauth_states"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_hash: Mapped[str]
    purpose: Mapped[str]
    session_hash: Mapped[str | None]
    identity_id: Mapped[str | None]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Run(Base):
    __tablename__ = "summary_runs"
    __table_args__ = (
        Index(
            "uq_scheduled_run",
            "user_id",
            "scheduled_at",
            unique=True,
            postgresql_where=text("kind = 'scheduled'"),
        ),
        Index("ix_runs_schedule", "scheduled_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str]
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_status: Mapped[str] = mapped_column(default="queued")
    coverage_status: Mapped[str] = mapped_column(default="unknown")
    delivery_status: Mapped[str] = mapped_column(default="not_started")
    auth_epoch: Mapped[int]
    config_version: Mapped[int]
    retry_count: Mapped[int] = mapped_column(default=0)
    lease_owner: Mapped[str | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fence: Mapped[int] = mapped_column(default=0, server_default="0")
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_parts_count: Mapped[int] = mapped_column(default=0, server_default="0")
    safe_error: Mapped[str | None]
    collection_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    coverage_issues: Mapped[list] = mapped_column(JSON, default=list)
    steps: Mapped[list] = mapped_column(JSON, default=list)


class ManualRequest(Base):
    __tablename__ = "manual_requests"
    __table_args__ = (UniqueConstraint("user_id", "key_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    key_hash: Mapped[str]
    run_id: Mapped[str] = mapped_column(ForeignKey("summary_runs.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Audit(Base):
    __tablename__ = "admin_access_audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str | None]
    action: Mapped[str]
    allowed: Mapped[bool]
    request_id: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db():
    with SessionLocal() as db:
        yield db


class DeliveryPart(Base):
    __tablename__ = "delivery_parts"
    __table_args__ = (UniqueConstraint("run_id", "purpose", "part"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("summary_runs.id"))
    purpose: Mapped[str] = mapped_column(default="report")
    part: Mapped[int]
    uuid: Mapped[str] = mapped_column(unique=True)
    payload_hash: Mapped[str]
    status: Mapped[str] = mapped_column(default="sending")
    first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[str | None]


class AuthNotice(Base):
    __tablename__ = "auth_notices"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    auth_epoch: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(default="sending")
    message_id: Mapped[str | None]
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserExecution(Base):
    __tablename__ = "user_execution_locks"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("summary_runs.id"))
    owner: Mapped[str]
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkerHealth(Base):
    __tablename__ = "worker_health"
    name: Mapped[str] = mapped_column(primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
