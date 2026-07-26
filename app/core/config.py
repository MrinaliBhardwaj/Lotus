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
    # Fail-closed default: an unconfigured deploy boots as prod and the
    # validators below refuse to start on dev secrets/wildcards. Local dev and
    # CI set APP_ENV explicitly (.env / test fixtures).
    app_env: Literal["dev", "test", "prod"] = "prod"
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
    # poison-input ceilings (H3): a crafted page can be under the page limit yet
    # explode block/table/text extraction memory — cap per page and fail cleanly
    parse_max_blocks_per_page: int = 10_000
    parse_max_chars_per_page: int = 2_000_000

    # --- chunking (pinned at scaffold review: 300-token children, 12% overlap,
    # measured with the embedding provider's tokenizer) -------------------------
    chunk_child_tokens: int = 300
    chunk_child_overlap_tokens: int = 36  # 12% of 300
    chunk_parent_tokens: int = 2000

    # --- embedding stage --------------------------------------------------------
    embed_batch_size: int = 128  # texts per provider request (≤ provider max)
    embed_requests_per_minute: int = 300  # token-bucket ceiling on provider calls

    # --- retrieval (Task 7: dense ∥ lexical → RRF → children → parents) ----------
    retrieval_dense_k: int = 30
    retrieval_lexical_k: int = 30
    retrieval_rrf_k: int = 60  # the RRF constant; consumes ranks only (§2.1 #10)
    retrieval_children_k: int = 12  # fused children kept before parent expansion
    retrieval_max_sources: int = 6  # parents handed to the LLM as [S#] sources

    # --- SSE progress (§2.1 #9) ---------------------------------------------------
    progress_stream_timeout_seconds: int = 600
    progress_poll_interval_seconds: float = 1.0  # DB-poll fallback when Redis is down

    # --- rate limiting (CLAUDE.md §2.1 #11) -------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_auth_per_minute: int = 20  # per client IP
    rate_limit_upload_per_minute: int = 60  # per authenticated user
    rate_limit_chat_per_minute: int = 20  # per user — caps LLM spend (C1)
    rate_limit_redis_retry_seconds: float = 30.0  # cooldown before retrying Redis (M1)

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

    @model_validator(mode="after")
    def _production_is_locked_down(self) -> "Settings":
        """In prod, refuse to boot on any dev default that would be a security
        hole (C2/M3/L3). These fire loudly at startup, never silently."""
        if self.app_env != "prod":
            return self
        problems: list[str] = []
        if self.jwt_secret_key == _DEV_JWT_SECRET:
            problems.append("JWT_SECRET_KEY is still the committed dev default")
        if "*" in self.cors_origins:
            problems.append("CORS_ORIGINS may not contain '*' (credentials are allowed)")
        if self.storage_backend == "s3" and (
            self.s3_access_key_id == "lexa-dev" or self.s3_secret_access_key == "lexa-dev-secret"
        ):
            problems.append("S3 credentials are still the dev defaults")
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        if self.embedding_provider == "openai" and not self.openai_api_key:
            problems.append("OPENAI_API_KEY is required when EMBEDDING_PROVIDER=openai")
        if problems:
            raise ValueError(
                "insecure production configuration — set APP_ENV=dev for local use, or fix: "
                + "; ".join(problems)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
