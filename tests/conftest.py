"""Shared fixtures.

Unit tests run with fake providers + local storage — no network, no API keys.
Schema tests (tests/test_schema.py) need Postgres: the ``migrated_db_url``
fixture creates a throwaway ``lexa_test`` database and applies the full Alembic
chain to it; those tests skip cleanly when no database is reachable. CI
provides a pgvector/pg16 service so they always run there.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.storage.local import LocalStorage

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_DB_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL", "postgresql://lexa:lexa@localhost:5432/lexa"
)
TEST_DB_NAME = "lexa_test"


def run_alembic(command: list[str], database_url: str) -> None:
    """Run alembic against an explicit DATABASE_URL in a subprocess so the
    lru_cached Settings of this test process are never involved."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *command],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic {' '.join(command)} failed:\n{result.stderr}"


@pytest.fixture(scope="session")
def migrated_db_url() -> str:
    """Async-driver URL of a freshly created lexa_test DB at migration head."""
    try:
        admin = psycopg.connect(ADMIN_DB_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("postgres unavailable — schema tests need a running database")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB_NAME}")
    base, _, _ = ADMIN_DB_URL.rpartition("/")
    async_url = f"{base}/{TEST_DB_NAME}".replace("postgresql://", "postgresql+asyncpg://")
    run_alembic(["upgrade", "head"], async_url)
    return async_url


@pytest.fixture
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(
        app_env="test",
        storage_backend="local",
        local_storage_path=str(tmp_path_factory.mktemp("storage")),
        llm_provider="fake",
        embedding_provider="fake",
        jwt_secret_key="test-secret-0123456789abcdef0123456789abcdef",
    )


@pytest.fixture
def local_storage(settings: Settings) -> Iterator[LocalStorage]:
    storage = LocalStorage(settings.local_storage_path)
    yield storage
    storage.wipe()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()
