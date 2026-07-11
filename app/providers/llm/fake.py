"""Deterministic fake for tests and keyless dev — never calls the network.

The canned answer cites [S1] (valid in any request with ≥1 source) AND a
fabricated [S99], so the structural citation validation path (invariant 5)
is exercised end-to-end. Streaming yields fixed-size slices, deliberately
splitting citation brackets across chunks.
"""

from collections.abc import AsyncIterator

from app.providers.llm.base import LLMProvider

_STREAM_SLICE = 7


class FakeLLMProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "fake-llm-v1"

    def _answer(self, user: str) -> str:
        question = user.rsplit("Question:", 1)[-1].strip() if "Question:" in user else user
        return (
            "Based on the provided sources, the agreement addresses this directly [S1]. "
            "A fabricated reference follows for validation testing [S99]. "
            f"FAKE_ANSWER({question[:80]})"
        )

    async def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> str:
        return self._answer(user)

    async def stream(
        self, *, system: str, user: str, max_tokens: int | None = None
    ) -> AsyncIterator[str]:
        text = self._answer(user)
        for start in range(0, len(text), _STREAM_SLICE):
            yield text[start : start + _STREAM_SLICE]
