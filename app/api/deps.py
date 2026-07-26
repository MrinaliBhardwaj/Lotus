"""Request-scoped FastAPI dependencies.

``get_current_user_id`` is the tenancy anchor (hard invariant #7): every
tenant-scoped endpoint takes it, and services filter every query by it. It is
real JWT verification from day one — not a stub — even though the register/
login endpoints only arrive with Task 3.
"""

import uuid
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import ratelimit
from app.core.config import Settings, get_settings
from app.core.deps import get_storage
from app.core.exceptions import AuthenticationError
from app.core.security import decode_access_token
from app.db.session import get_async_session_factory, get_db_session
from app.storage.base import ObjectStorage

_bearer = HTTPBearer(auto_error=False)

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Session factory for streaming endpoints that must NOT hold a
    request-scoped connection for the life of the stream (H1). They open a
    short-lived session per DB touch instead. Overridable in tests so the
    factory points at the test engine, same as ``get_db_session``."""
    return get_async_session_factory()


SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


async def get_current_user_id(
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> uuid.UUID:
    if credentials is None:
        raise AuthenticationError("missing bearer token")
    return decode_access_token(credentials.credentials, settings)


CurrentUserId = Annotated[uuid.UUID, Depends(get_current_user_id)]


def get_storage_dep(settings: SettingsDep) -> ObjectStorage:
    # the cached singleton in prod; a fresh instance when tests inject Settings
    return get_storage() if settings is get_settings() else get_storage(settings)


StorageDep = Annotated[ObjectStorage, Depends(get_storage_dep)]


async def auth_rate_limit(request: Request, settings: SettingsDep) -> None:
    """Per-client-IP limit on the unauthenticated auth endpoints (§2.1 #11)."""
    if not settings.rate_limit_enabled:
        return
    client_ip = request.client.host if request.client else "unknown"
    await ratelimit.get_limiter().hit(
        f"auth:{client_ip}", settings.rate_limit_auth_per_minute, window_seconds=60
    )


async def upload_rate_limit(user_id: CurrentUserId, settings: SettingsDep) -> None:
    """Per-user limit on the upload/finalize endpoints. FastAPI caches the
    ``get_current_user_id`` sub-dependency, so the token is decoded once."""
    if not settings.rate_limit_enabled:
        return
    await ratelimit.get_limiter().hit(
        f"upload:{user_id}", settings.rate_limit_upload_per_minute, window_seconds=60
    )


async def chat_rate_limit(user_id: CurrentUserId, settings: SettingsDep) -> None:
    """Per-user limit on the ask endpoint — every call costs an embedding plus
    an LLM completion, so this is the primary spend/abuse control (C1)."""
    if not settings.rate_limit_enabled:
        return
    await ratelimit.get_limiter().hit(
        f"chat:{user_id}", settings.rate_limit_chat_per_minute, window_seconds=60
    )
