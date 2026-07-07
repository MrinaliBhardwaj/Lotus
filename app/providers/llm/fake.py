"""Deterministic fake for tests and keyless dev — never calls the network."""

from collections.abc import AsyncIterator

from app.providers.llm.base import LLMProvider


class FakeLLMProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "fake-llm-v1"

    async def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> str:
        return f"FAKE_ANSWER({user[:80]})"

    async def stream(
        self, *, system: str, user: str, max_tokens: int | None = None
    ) -> AsyncIterator[str]:
        for chunk in ("FAKE_", "ANSWER(", user[:80], ")"):
            yield chunk
