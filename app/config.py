from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    public_base_url: str = "http://localhost:5173"
    database_url: str = "postgresql+psycopg://summary:summary@localhost:5432/summary"
    allowed_tenant_key: str = "local-demo"
    pilot_open_ids: str = ""
    admin_open_ids: str = ""
    mock_auth_enabled: bool = False
    # Real collection stays gated until the tenant's two-user PoC has passed.
    live_pipeline_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret_file: str = ""
    token_encryption_key_file: str = ""
    session_lifetime_days: int = Field(default=90, ge=1, le=365)
    model_base_url: str = ""
    model_id: str = ""
    model_token_budget: int = Field(default=200000, ge=10000, le=2000000)
    model_api_key_file: str = ""
    maintenance_mode: bool = False
    payload_dir: str = "/run/summary-data"

    # 启动时校验环境组合：演示仅限本机，生产要求 HTTPS 和凭证，真实流程必须配置模型。
    @model_validator(mode="after")
    def validate_environment(self):
        url = urlparse(self.public_base_url)
        if url.path not in ("", "/") or url.query or url.fragment or not url.hostname:
            raise ValueError("PUBLIC_BASE_URL must be an origin")
        self.public_base_url = self.public_base_url.rstrip("/")
        if self.mock_auth_enabled and (
            self.app_env != "development" or url.hostname not in ("localhost", "127.0.0.1")
        ):
            raise ValueError("Mock authentication is restricted to local development")
        if self.app_env == "production":
            if (
                url.scheme != "https"
                or not self.allowed_tenant_key
                or self.allowed_tenant_key == "local-demo"
            ):
                raise ValueError("Production requires HTTPS and a real tenant")
            if (
                not self.feishu_app_id
                or not self.feishu_app_secret_file
                or not self.token_encryption_key_file
            ):
                raise ValueError("Production requires Feishu and encryption secrets")
        if self.live_pipeline_enabled:
            if self.mock_auth_enabled:
                raise ValueError("Live pipeline and mock mode cannot both be enabled")
            if (
                not self.model_base_url.startswith("https://")
                or not self.model_id
                or not self.model_api_key_file
            ):
                raise ValueError(
                    "Live pipeline requires an approved HTTPS model endpoint and credentials"
                )
        return self

    # 将配置中的逗号分隔试点名单转换为去空白的集合。
    @property
    def pilots(self) -> set[str]:
        return {x.strip() for x in self.pilot_open_ids.split(",") if x.strip()}

    # 将管理员名单转换为集合，用于每次请求的实时权限判断。
    @property
    def admins(self) -> set[str]:
        return {x.strip() for x in self.admin_open_ids.split(",") if x.strip()}

    # HTTPS 使用带 Host 前缀的会话 Cookie，本机 HTTP 使用开发名称。
    @property
    def cookie_name(self) -> str:
        return "__Host-session" if self.public_base_url.startswith("https://") else "dev-session"

    # 按配置指定的文件路径读取密钥，不在环境配置中直接保存密钥正文。
    def secret(self, field: str) -> str:
        path = getattr(self, field)
        if not path:
            raise ValueError(f"Missing secret file: {field}")
        return Path(path).read_text().strip()


# 缓存进程配置，避免每个请求重复读取；修改配置后需重启相应进程。
@lru_cache
def get_settings() -> Settings:
    return Settings()
