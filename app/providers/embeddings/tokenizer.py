"""Tokenizers for chunk sizing (pinned: ~300-token children, 12% overlap).

Chunk sizes are measured with the embedder's own tokenizer, so the tokenizer
hangs off the embedding provider (invariant 4 — vendor specifics stay behind
the provider abstraction). The OpenAI provider uses tiktoken ``cl100k_base``
(the ``text-embedding-3-small`` tokenizer, loaded lazily because it fetches
its vocab on first use); the fake provider uses an offline word tokenizer so
tests and CI never touch the network.
"""

import re
from abc import ABC, abstractmethod
from itertools import accumulate

import tiktoken


class Tokenizer(ABC):
    @abstractmethod
    def count(self, text: str) -> int:
        """Number of tokens in ``text``."""

    @abstractmethod
    def split(self, text: str, chunk_tokens: int, overlap_tokens: int) -> list[tuple[int, int]]:
        """Split into windows of ≤ ``chunk_tokens`` tokens overlapping by
        ``overlap_tokens``, returned as (char_start, char_end) ranges into
        ``text`` — provenance offsets, never reconstructed text."""


class TiktokenTokenizer(Tokenizer):
    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self._encoding_name = encoding_name
        self._encoding: tiktoken.Encoding | None = None

    @property
    def _enc(self) -> tiktoken.Encoding:
        if self._encoding is None:
            self._encoding = tiktoken.get_encoding(self._encoding_name)
        return self._encoding

    def count(self, text: str) -> int:
        return len(self._enc.encode(text, disallowed_special=()))

    def split(self, text: str, chunk_tokens: int, overlap_tokens: int) -> list[tuple[int, int]]:
        ids = self._enc.encode(text, disallowed_special=())
        if len(ids) <= chunk_tokens:
            return [(0, len(text))] if text else []
        # byte offset of each token boundary (BPE tokens partition the bytes)
        cumulative = [0, *accumulate(len(self._enc.decode_single_token_bytes(t)) for t in ids)]
        data = text.encode("utf-8")

        def to_char(byte_offset: int) -> int:
            # a boundary can split a multi-byte char; "ignore" rounds down safely
            return len(data[:byte_offset].decode("utf-8", errors="ignore"))

        stride = max(chunk_tokens - overlap_tokens, 1)
        ranges: list[tuple[int, int]] = []
        for start in range(0, len(ids), stride):
            end = min(start + chunk_tokens, len(ids))
            ranges.append((to_char(cumulative[start]), to_char(cumulative[end])))
            if end == len(ids):
                break
        return ranges


class WordTokenizer(Tokenizer):
    """Deterministic offline approximation (~1 token per word) for the fake
    embedding provider. Same interface, no vocab download."""

    _WORDS = re.compile(r"\S+")

    def count(self, text: str) -> int:
        return len(self._WORDS.findall(text))

    def split(self, text: str, chunk_tokens: int, overlap_tokens: int) -> list[tuple[int, int]]:
        words = list(self._WORDS.finditer(text))
        if len(words) <= chunk_tokens:
            return [(0, len(text))] if text else []
        stride = max(chunk_tokens - overlap_tokens, 1)
        ranges: list[tuple[int, int]] = []
        for start in range(0, len(words), stride):
            end = min(start + chunk_tokens, len(words))
            ranges.append((words[start].start(), words[end - 1].end()))
            if end == len(words):
                break
        return ranges
