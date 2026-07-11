"""OpenAI implementation (pinned default: text-embedding-3-small / 1536 — §2.1 #5)."""

from collections.abc import Sequence

import openai

from app.core.config import Settings
from app.core.exceptions import ProviderError
from app.providers.embeddings.base import EmbeddingProvider
from app.providers.embeddings.tokenizer import TiktokenTokenizer, Tokenizer


class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(self, settings: Settings) -> None:
        self._client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        # cl100k_base is the text-embedding-3-* tokenizer; lazy vocab load
        self._tokenizer = TiktokenTokenizer("cl100k_base")

    @property
    def model(self) -> str:
        return self._model

    @property
    def tokenizer(self) -> Tokenizer:
        return self._tokenizer

    @property
    def version(self) -> str:
        return "1"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def max_batch_size(self) -> int:
        return 2048

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > self.max_batch_size:
            raise ProviderError(f"batch of {len(texts)} exceeds max {self.max_batch_size}")
        try:
            response = await self._client.embeddings.create(
                model=self._model, input=list(texts), dimensions=self._dimensions
            )
        except openai.OpenAIError as exc:
            raise ProviderError(f"openai embedding failed: {exc.__class__.__name__}") from exc
        # order-preserving by index, defensively re-sorted
        ordered = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in ordered]
