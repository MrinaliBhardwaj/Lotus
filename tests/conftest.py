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
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.models import JobStage
from app.storage.local import LocalStorage
from app.workers.session import set_session_factory

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


# --- DB-backed API fixtures (Tasks 3+) ----------------------------------------


@pytest.fixture
def db_settings(migrated_db_url: str, tmp_path: Path) -> Settings:
    """Settings pointing every dependency at test-safe backends."""
    return Settings(
        app_env="test",
        database_url=migrated_db_url,  # type: ignore[arg-type]
        storage_backend="local",
        local_storage_path=str(tmp_path / "storage"),
        local_public_base_url="",  # relative upload URLs → same test client
        llm_provider="fake",
        embedding_provider="fake",
        jwt_secret_key="test-secret-0123456789abcdef0123456789abcdef",
        rate_limit_enabled=False,
    )


@pytest.fixture
async def db_app(db_settings: Settings, migrated_db_url: str) -> AsyncIterator[FastAPI]:
    """The real app wired to the migrated test database via dependency overrides.

    NullPool: each test runs in its own event loop, and pooled asyncpg
    connections must not leak across loops.
    """
    from app.core.config import get_settings
    from app.db.session import get_db_session
    from app.main import create_app

    engine = create_async_engine(migrated_db_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: db_settings
    app.dependency_overrides[get_db_session] = _session
    yield app
    await engine.dispose()


@pytest.fixture
async def db_client(db_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=db_app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


@pytest.fixture
def captured_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[JobStage, uuid.UUID]]:
    """Intercept Celery hand-offs so tests run without a broker."""
    calls: list[tuple[JobStage, uuid.UUID]] = []

    def _capture(stage: JobStage, document_id: uuid.UUID) -> None:
        calls.append((stage, document_id))

    monkeypatch.setattr("app.services.ingestion.pipeline.enqueue_stage", _capture)
    return calls


@pytest.fixture
def inline_pipeline(
    monkeypatch: pytest.MonkeyPatch, db_settings: Settings
) -> list[JobStage]:
    """Run the whole ingestion pipeline synchronously in-process: every stage
    hand-off executes the stage function inline instead of hitting Celery."""
    import uuid as uuid_module

    from app.parsers.pymupdf_parser import PyMuPDFParser
    from app.services.ingestion import pipeline as pipeline_module
    from app.services.ingestion.chunking import run_chunk
    from app.services.ingestion.parse import run_parse, run_parse_batch
    from app.services.ingestion.structure import run_structure
    from app.services.ingestion.validate import run_validate

    storage = LocalStorage(db_settings.local_storage_path)
    parser = PyMuPDFParser()
    executed: list[JobStage] = []

    def _run_stage(stage: JobStage, document_id: uuid_module.UUID) -> None:
        executed.append(stage)
        if stage is JobStage.VALIDATE:
            run_validate(document_id, settings=db_settings, storage=storage)
        elif stage is JobStage.PARSE:
            run_parse(document_id, settings=db_settings)
        elif stage is JobStage.STRUCTURE:
            run_structure(document_id, settings=db_settings, storage=storage)
        elif stage is JobStage.CHUNK:
            run_chunk(document_id, settings=db_settings, storage=storage)
        elif stage is JobStage.EMBED:
            from app.services.ingestion.embed import run_embed

            run_embed(document_id, settings=db_settings, storage=storage)
        elif stage is JobStage.INDEX:
            from app.services.ingestion.embed import run_index

            run_index(document_id, settings=db_settings)

    def _run_batch(document_id: uuid_module.UUID, start: int, end: int) -> None:
        run_parse_batch(
            document_id, start, end, settings=db_settings, storage=storage, parser=parser
        )

    monkeypatch.setattr(pipeline_module, "enqueue_stage", _run_stage)
    monkeypatch.setattr(pipeline_module, "enqueue_parse_batch", _run_batch)
    return executed


@pytest.fixture
def sync_session_factory(migrated_db_url: str) -> Iterator[sessionmaker[Session]]:
    """Worker-style sync sessions bound to the test DB (§2.1 #7)."""
    sync_url = migrated_db_url.replace("+asyncpg", "+psycopg")
    engine = create_engine(sync_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    set_session_factory(factory)
    yield factory
    set_session_factory(None)
    engine.dispose()
