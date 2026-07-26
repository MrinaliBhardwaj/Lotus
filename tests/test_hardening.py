"""Security-hardening regressions: prod config fail-closed (C2/M3/L3), security
headers (L5), poison-input parser ceilings (H3), presigned-upload shape (H2),
and the rate-limiter self-heal (M1)."""

import time

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.exceptions import ParserError, RateLimitedError
from app.core.ratelimit import FixedWindowLimiter
from app.parsers.pymupdf_parser import PyMuPDFParser
from tests.pdf_utils import make_pdf

_REAL_SECRET = "prod-secret-0123456789abcdef0123456789abcdef"


def _prod(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "prod",
        "jwt_secret_key": _REAL_SECRET,
        "storage_backend": "local",
        "llm_provider": "fake",
        "embedding_provider": "fake",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- C2/M3/L3: production refuses dev defaults --------------------------------


def test_prod_rejects_dev_jwt_secret() -> None:
    with pytest.raises(ValidationError, match="dev default"):
        _prod(jwt_secret_key="dev-only-not-a-secret-change-me-padding-0000")


def test_prod_rejects_wildcard_cors() -> None:
    with pytest.raises(ValidationError, match="CORS"):
        _prod(cors_origins=["*"])


def test_prod_requires_provider_keys() -> None:
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        _prod(llm_provider="anthropic")
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        _prod(embedding_provider="openai")


def test_prod_rejects_dev_s3_credentials() -> None:
    with pytest.raises(ValidationError, match="S3 credentials"):
        _prod(storage_backend="s3", s3_access_key_id="lexa-dev", s3_secret_access_key="x" * 8)


def test_a_clean_prod_config_boots() -> None:
    settings = _prod(cors_origins=["https://app.example.com"])
    assert settings.app_env == "prod"


def test_dev_default_allows_dev_secret() -> None:
    # local dev keeps its ergonomics — the dev secret does not raise when APP_ENV=dev
    from app.core.config import _DEV_JWT_SECRET

    settings = Settings(app_env="dev", jwt_secret_key=_DEV_JWT_SECRET)
    assert settings.app_env == "dev"


# --- L5: security headers on every response -----------------------------------


async def test_security_headers_present() -> None:
    from app.main import create_app

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


# --- H3: poison-input parser ceilings -----------------------------------------


def test_parser_rejects_too_many_blocks() -> None:
    parser = PyMuPDFParser(max_blocks_per_page=0)
    with pytest.raises(ParserError, match="blocks"):
        parser.parse_pages(make_pdf(pages=1), [1])


def test_parser_rejects_too_many_chars() -> None:
    parser = PyMuPDFParser(max_chars_per_page=1)
    with pytest.raises(ParserError, match="chars"):
        parser.parse_pages(make_pdf(pages=1), [1])


def test_parser_accepts_normal_page_within_ceilings() -> None:
    parser = PyMuPDFParser()  # generous defaults
    pages = parser.parse_pages(make_pdf(pages=1), [1])
    assert pages and pages[0].blocks


# --- M1: rate limiter self-heals instead of latching to memory forever --------


async def test_limiter_time_boxes_redis_degradation() -> None:
    limiter = FixedWindowLimiter("redis://127.0.0.1:1/0", retry_after_seconds=0.3)
    # first hit fails Redis and falls to memory, arming a retry window
    await limiter.hit("k", limit=100, window_seconds=60)
    assert limiter._redis_retry_at > time.monotonic()
    # inside the window it stays on memory (does not thrash Redis every call)
    before = limiter._redis_retry_at
    await limiter.hit("k", limit=100, window_seconds=60)
    assert limiter._redis_retry_at == before
    # after the cooldown it will try Redis again (re-arming on failure)
    time.sleep(0.35)
    await limiter.hit("k", limit=100, window_seconds=60)
    assert limiter._redis_retry_at > before


async def test_limiter_still_enforces_while_degraded() -> None:
    limiter = FixedWindowLimiter("redis://127.0.0.1:1/0", retry_after_seconds=30)
    for _ in range(3):
        await limiter.hit("k", limit=3, window_seconds=60)
    with pytest.raises(RateLimitedError):
        await limiter.hit("k", limit=3, window_seconds=60)
