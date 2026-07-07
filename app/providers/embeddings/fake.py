"""Deterministic fake embeddings for tests — stable across runs, no network."""

import hashlib
import math
from collections.abc import Sequence

from app.providers.embeddings.base import EmbeddingProvider


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, dimensions: int = 1536) -> None:
        self._dimensions = dimensions

    @property
    def model(self) -> str:
        return "fake-embedding-v1"

    @property
    def version(self) -> str:
        return "1"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def max_batch_size(self) -> int:
        return 512

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        # Expand a SHA-256 of the text into a unit vector: deterministic, and
        # identical texts embed identically (mirrors real content-hash dedupe).
        values: list[float] = []
        counter = 0
        while len(values) < self._dimensions:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            values.extend(byte / 255.0 - 0.5 for byte in digest)
            counter += 1
        values = values[: self._dimensions]
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]
