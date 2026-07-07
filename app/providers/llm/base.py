"""LLM provider interface.

Deliberately minimal for Phase 1: grounded answering (Task 7) needs a system
prompt, a user message, and streaming. The interface never leaks vendor types.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class LLMProvider(ABC):
    @property
    @abstractmethod
    def model(self) -> str:
        """Identifier of the underlying model (persisted for reproducibility)."""

    @abstractmethod
    async def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> str:
        """Single-shot completion; returns the full response text."""

    @abstractmethod
    def stream(
        self, *, system: str, user: str, max_tokens: int | None = None
    ) -> AsyncIterator[str]:
        """Streaming completion; yields text deltas."""
