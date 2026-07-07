from app.core.config import Settings
from app.core.deps import get_embedding_provider, get_llm_provider, get_storage
from app.providers.embeddings.fake import FakeEmbeddingProvider
from app.providers.llm.fake import FakeLLMProvider
from app.storage.local import LocalStorage


async def test_fake_embeddings_are_deterministic_and_pinned_dimension() -> None:
    provider = FakeEmbeddingProvider(dimensions=1536)
    [a1], [a2] = await provider.embed(["same text"]), await provider.embed(["same text"])
    [b] = await provider.embed(["different text"])
    assert a1 == a2  # content-hash dedupe relies on this
    assert a1 != b
    assert len(a1) == 1536  # matches vector(1536) (§2.1 #5)


async def test_fake_llm_streams_and_completes() -> None:
    provider = FakeLLMProvider()
    full = await provider.complete(system="s", user="question")
    streamed = "".join([chunk async for chunk in provider.stream(system="s", user="question")])
    assert full == streamed


def test_config_selects_fake_implementations(settings: Settings) -> None:
    assert isinstance(get_llm_provider(settings), FakeLLMProvider)
    assert isinstance(get_embedding_provider(settings), FakeEmbeddingProvider)
    assert isinstance(get_storage(settings), LocalStorage)


def test_config_selects_real_implementations() -> None:
    from app.providers.embeddings.openai_provider import OpenAIEmbeddingProvider
    from app.providers.llm.anthropic_provider import AnthropicLLMProvider
    from app.storage.s3 import S3Storage

    real = Settings(
        app_env="test",
        llm_provider="anthropic",
        embedding_provider="openai",
        storage_backend="s3",
        anthropic_api_key="test-key",
        openai_api_key="test-key",
    )
    assert isinstance(get_llm_provider(real), AnthropicLLMProvider)
    assert isinstance(get_embedding_provider(real), OpenAIEmbeddingProvider)
    assert isinstance(get_storage(real), S3Storage)
