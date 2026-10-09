"""Explicit API response allowlists; this module is the frontend contract source."""

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    data: T
    request_id: str


class Person(BaseModel):
    name: str
    open_id: str


class Capabilities(BaseModel):
    personal: bool
    admin: bool


class SessionInfo(BaseModel):
    identity: Person
    capabilities: Capabilities
    authorization: str
    csrf_token: str
    demo: bool


class SettingsView(BaseModel):
    authorization: str
    enabled_status: str
    daily_time: str
    timezone: str
    next_run_at: datetime | None


class UserView(SettingsView):
    user_id: str
    name: str
    open_id: str
    auth_status: str
    first_authorized_at: datetime
    last_authorized_at: datetime
    auth_checked_at: datetime
    latest_run_result: str | None


class Step(BaseModel):
    stage: str
    at: str


class Part(BaseModel):
    part: int
    status: str
    message_id: str | None


class RunView(BaseModel):
    run_id: str
    user: Person
    kind: str
    scheduled_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    window_start: datetime
    window_end: datetime
    execution_status: str
    coverage_status: str
    delivery_status: str
    duration_ms: int | None


class RunDetail(RunView):
    safe_error: str | None
    retry_count: int
    steps: list[Step]
    collection_counts: dict[str, int]
    coverage_issues: list[str]
    delivery_parts: list[Part]


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class RunCreated(BaseModel):
    run_id: str


class DemoAccount(BaseModel):
    label: str
    login_url: str


class LoginOptions(BaseModel):
    demo: bool
    feishu_enabled: bool
    accounts: list[DemoAccount]
