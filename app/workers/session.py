"""Worker-side DB session handling (CLAUDE.md §2.1 #7).

Celery workers are synchronous and use their own sync engine/session factory —
never the API's async engine. ``worker_session`` is the single entry point
stage code uses; tests swap the factory via ``set_session_factory`` to point
workers at the migrated test database.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_sync_session_factory

_factory_override: sessionmaker[Session] | None = None


def set_session_factory(factory: sessionmaker[Session] | None) -> None:
    """Test seam: route worker sessions at an explicit engine (None resets)."""
    global _factory_override
    _factory_override = factory


@contextmanager
def worker_session() -> Iterator[Session]:
    factory = _factory_override or get_sync_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()
