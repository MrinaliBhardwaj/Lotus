"""Application settings, validated from the environment (CLAUDE.md §3).

All configuration flows through :class:`Settings`. No module reads ``os.environ``
directly, and no secrets are hardcoded anywhere in the codebase.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_JWT_SECRET = "dev-only-not-a-secret-change-me-padding-0000"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "lexa"
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    # --- database -------------------------------------------------------
    database_url: PostgresDsn = PostgresDsn("postgresql+asyncpg://lexa:lexa@localhost:5432/lexa")

    @property
    def database_url_sync(self) -> str:
        """Sync-driver URL for Celery workers (CLAUDE.md §2.1 #7)."""
        return str(self.database_url).replace("postgresql+asyncpg://", "postgresql+psycopg://")

    # --- redis ------------------------------------------------------------
    redis_url: RedisDsn = RedisDsn("redis://localhost:6379/0")

    # --- auth (pinned: argon2id + JWT HS256 — CLAUDE.md §2.1 #1) -----------
    # >= 32 bytes per RFC 7518 §3.2 for HS256
    jwt_secret_key: str = Field(default=_DEV_JWT_SECRET, min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60

    @model_validator(mode="after")
    def _no_dev_secret_in_prod(self) -> "Settings":
        if self.app_env == "prod" and self.jwt_secret_key == _DEV_JWT_SECRET:
            raise ValueError("JWT_SECRET_KEY must be set to a real secret when APP_ENV=prod")
        return self

    # --- API hardening ------------------------------------------------------
    cors_origins: list[str] = ["http://localhost:3000"]
    max_request_body_bytes: int = 10 * 1024 * 1024  # 10 MiB JSON bodies; uploads bypass the API

    # --- object storage ------------------------------------------------------
    storage_backend: Literal["s3", "local"] = "s3"
    s3_endpoint_url: str | None = "http://localhost:9000"  # None => real AWS
    s3_bucket: str = "lexa-dev"
    s3_access_key_id: str = "lexa-dev"
    s3_secret_access_key: str = "lexa-dev-secret"
    s3_region: str = "us-east-1"
    s3_presign_expiry_seconds: int = 900
    local_storage_path: str = ".localstorage"
    # base URL prefixed to local-adapter upload URLs so a browser can PUT to them
    local_public_base_url: str = "http://localhost:8000"
    max_upload_bytes: int = 500 * 1024 * 1024  # 500 MiB PDF ceiling

    # --- ingestion limits ------------------------------------------------------
    max_pdf_pages: int = 1500  # DESIGN targets 500–1000-page docs; hard ceiling above that
    parse_batch_pages: int = 50  # pages per parallel parse task

    # --- chunking (pinned at scaffold review: 300-token children, 12% overlap,
    # measured with the embedding provider's tokenizer) -------------------------
    chunk_child_tokens: int = 300
    chunk_child_overlap_tokens: int = 36  # 12% of 300
    chunk_parent_tokens: int = 2000

    # --- rate limiting (CLAUDE.md §2.1 #11) -------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_auth_per_minute: int = 20  # per client IP
    rate_limit_upload_per_minute: int = 60  # per authenticated user

    # --- providers (pinned: CLAUDE.md §2.1 #5/#6) -----------------------------
    llm_provider: Literal["anthropic", "fake"] = "anthropic"
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-4-8"
    llm_max_tokens: int = 4096

    embedding_provider: Literal["openai", "fake"] = "openai"
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # --- Celery hardening (CLAUDE.md §2.1 #8) ---------------------------------
    celery_task_time_limit: int = 1800
    celery_task_soft_time_limit: int = 1500
    celery_max_memory_per_child_kb: int = 1024 * 1024  # 1 GiB


@lru_cache
def get_settings() -> Settings:
    return Settings()
