"""Task 3 gate tests: register/login and the auth boundary."""

import uuid

import pytest
from httpx import AsyncClient

from app.core.exceptions import RateLimitedError
from app.core.ratelimit import FixedWindowLimiter


def _credentials() -> dict[str, str]:
    return {"email": f"{uuid.uuid4().hex}@example.com", "password": "correct-horse-battery"}


async def test_register_login_roundtrip(db_client: AsyncClient) -> None:
    creds = _credentials()
    registered = await db_client.post("/auth/register", json=creds)
    assert registered.status_code == 201
    body = registered.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == creds["email"]

    logged_in = await db_client.post("/auth/login", json=creds)
    assert logged_in.status_code == 200
    token = logged_in.json()["access_token"]

    documents = await db_client.get("/documents", headers={"Authorization": f"Bearer {token}"})
    assert documents.status_code == 200
    assert documents.json() == []


async def test_register_duplicate_email_conflict(db_client: AsyncClient) -> None:
    creds = _credentials()
    assert (await db_client.post("/auth/register", json=creds)).status_code == 201
    duplicate = await db_client.post("/auth/register", json=creds)
    assert duplicate.status_code == 409


async def test_login_wrong_password_rejected(db_client: AsyncClient) -> None:
    creds = _credentials()
    await db_client.post("/auth/register", json=creds)
    bad = await db_client.post(
        "/auth/login", json={"email": creds["email"], "password": "not-the-password"}
    )
    assert bad.status_code == 401
    # unknown email gets the same answer — no account-enumeration signal
    unknown = await db_client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "whatever-here"}
    )
    assert unknown.status_code == 401


async def test_endpoints_require_bearer_token(db_client: AsyncClient) -> None:
    assert (await db_client.get("/documents")).status_code == 401
    assert (await db_client.post("/documents", json={"title": "x"})).status_code == 401


async def test_garbage_token_rejected(db_client: AsyncClient) -> None:
    response = await db_client.get(
        "/documents", headers={"Authorization": "Bearer not.a.token"}
    )
    assert response.status_code == 401


async def test_rate_limiter_blocks_after_limit() -> None:
    # port 1 is never listening → limiter degrades to its in-memory window
    limiter = FixedWindowLimiter("redis://127.0.0.1:1/0")
    for _ in range(3):
        await limiter.hit("k", limit=3, window_seconds=60)
    with pytest.raises(RateLimitedError):
        await limiter.hit("k", limit=3, window_seconds=60)
    # other keys are unaffected
    await limiter.hit("other", limit=3, window_seconds=60)


async def test_auth_endpoint_returns_429_when_limited(
    db_client: AsyncClient,
    db_settings: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import ratelimit

    limiter = FixedWindowLimiter("redis://127.0.0.1:1/0")
    monkeypatch.setattr(ratelimit, "get_limiter", lambda: limiter)
    db_settings.rate_limit_enabled = True  # type: ignore[attr-defined]
    db_settings.rate_limit_auth_per_minute = 2  # type: ignore[attr-defined]

    payload = {"email": "limited@example.com", "password": "wrong-password-1"}
    statuses = [
        (await db_client.post("/auth/login", json=payload)).status_code for _ in range(3)
    ]
    assert statuses[:2] == [401, 401]
    assert statuses[2] == 429
