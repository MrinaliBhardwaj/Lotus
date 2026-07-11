"""Dependency wiring: settings → concrete provider/storage instances.

These factories are the single place where configuration selects an
implementation. Everything downstream depends only on the interfaces.
The no-argument path (used by the app) caches singletons; passing explicit
settings (tests) always builds a fresh instance.
"""

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.providers.embeddings.base import EmbeddingProvider
from app.providers.embeddings.fake import FakeEmbeddingProvider
from app.providers.embeddings.openai_provider import OpenAIEmbeddingProvider
from app.providers.llm.anthropic_provider import AnthropicLLMProvider
from app.providers.llm.base import LLMProvider
from app.providers.llm.fake import FakeLLMProvider
from app.storage.base import ObjectStorage
from app.storage.local import LocalStorage
from app.storage.s3 import S3Storage


def _build_storage(settings: Settings) -> ObjectStorage:
    if settings.storage_backend == "local":
        return LocalStorage(settings.local_storage_path, settings.local_public_base_url)
    return S3Storage(settings)


def _build_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "fake":
        return FakeLLMProvider()
    return AnthropicLLMProvider(settings)


def _build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "fake":
        return FakeEmbeddingProvider(settings.embedding_dimensions)
    return OpenAIEmbeddingProvider(settings)


@lru_cache
def _default_storage() -> ObjectStorage:
    return _build_storage(get_settings())


@lru_cache
def _default_llm_provider() -> LLMProvider:
    return _build_llm_provider(get_settings())


@lru_cache
def _default_embedding_provider() -> EmbeddingProvider:
    return _build_embedding_provider(get_settings())


def get_storage(settings: Settings | None = None) -> ObjectStorage:
    return _build_storage(settings) if settings is not None else _default_storage()


def get_llm_provider(settings: Settings | None = None) -> LLMProvider:
    return _build_llm_provider(settings) if settings is not None else _default_llm_provider()


def get_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    if settings is not None:
        return _build_embedding_provider(settings)
    return _default_embedding_provider()
