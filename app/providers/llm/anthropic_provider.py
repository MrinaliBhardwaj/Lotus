"""Anthropic implementation (pinned default: claude-opus-4-8 — CLAUDE.md §2.1 #6)."""

from collections.abc import AsyncIterator

import anthropic

from app.core.config import Settings
from app.core.exceptions import ProviderError
from app.providers.llm.base import LLMProvider


class AnthropicLLMProvider(LLMProvider):
    def __init__(self, settings: Settings) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.llm_model
        self._default_max_tokens = settings.llm_max_tokens

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> str:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens or self._default_max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APIError as exc:
            raise ProviderError(f"anthropic completion failed: {exc.__class__.__name__}") from exc
        return "".join(block.text for block in response.content if block.type == "text")

    async def stream(
        self, *, system: str, user: str, max_tokens: int | None = None
    ) -> AsyncIterator[str]:
        try:
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=max_tokens or self._default_max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except anthropic.APIError as exc:
            raise ProviderError(f"anthropic stream failed: {exc.__class__.__name__}") from exc
