"""Embedding provider interface.

``model``/``version`` are persisted per chunk (DESIGN.md §8) so re-embedding is
selective; ``dimensions`` must match the pinned ``vector(N)`` column (§2.1 #5).
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.providers.embeddings.tokenizer import Tokenizer


class EmbeddingProvider(ABC):
    @property
    @abstractmethod
    def model(self) -> str: ...

    @property
    @abstractmethod
    def tokenizer(self) -> Tokenizer:
        """The tokenizer chunk sizes are measured with (pinned per provider)."""

    @property
    @abstractmethod
    def version(self) -> str:
        """Bumped when the same model would produce different vectors (provider revision)."""

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

    @property
    @abstractmethod
    def max_batch_size(self) -> int:
        """Provider's maximum number of inputs per request (batching happens in Task 6)."""

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed up to ``max_batch_size`` texts; order-preserving."""
