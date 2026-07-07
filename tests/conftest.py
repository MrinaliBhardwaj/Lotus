"""Shared fixtures. Tests run with fake providers + local storage — no network,
no API keys, no running Postgres required for the Task 1 slice."""

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.storage.local import LocalStorage


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
