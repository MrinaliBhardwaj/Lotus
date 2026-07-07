"""Engines and session factories.

API path: async engine (asyncpg). Celery workers: a separate synchronous
engine (psycopg) — pinned in CLAUDE.md §2.1 #7. The two are never shared: an
async engine's connection pool is bound to the API's event loop and cannot be
safely used from worker processes.
"""

from collections.abc import AsyncIterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings

_async_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None

_sync_engine: Engine | None = None
_sync_session_factory: sessionmaker[Session] | None = None


def get_async_engine(settings: Settings | None = None) -> AsyncEngine:
    global _async_engine
    if _async_engine is None:
        settings = settings or get_settings()
        _async_engine = create_async_engine(str(settings.database_url), pool_pre_ping=True)
    return _async_engine


def get_async_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            get_async_engine(settings), expire_on_commit=False
        )
    return _async_session_factory


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, closed afterwards."""
    async with get_async_session_factory()() as session:
        yield session


def get_sync_engine(settings: Settings | None = None) -> Engine:
    """Worker-side engine (CLAUDE.md §2.1 #7). Never import from API code."""
    global _sync_engine
    if _sync_engine is None:
        settings = settings or get_settings()
        _sync_engine = create_engine(settings.database_url_sync, pool_pre_ping=True)
    return _sync_engine


def get_sync_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _sync_session_factory
    if _sync_session_factory is None:
        _sync_session_factory = sessionmaker(get_sync_engine(settings), expire_on_commit=False)
    return _sync_session_factory


async def dispose_engines() -> None:
    """Lifespan shutdown hook."""
    global _async_engine, _async_session_factory, _sync_engine, _sync_session_factory
    if _async_engine is not None:
        await _async_engine.dispose()
    if _sync_engine is not None:
        _sync_engine.dispose()
    _async_engine = None
    _async_session_factory = None
    _sync_engine = None
    _sync_session_factory = None
