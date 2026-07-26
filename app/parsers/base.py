"""PDF parser interface.

Kept out of ``services/`` deliberately (CLAUDE.md §3): parsing produces the
immutable IR and nothing else — chunking logic must never leak in here
(invariant 3). The interface exists so an ``unstructured`` fallback can slot
in behind the same contract.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path

from app.schemas.ir import PageIR


class PDFParser(ABC):
    @abstractmethod
    def parse_pages(self, pdf_bytes: bytes, page_numbers: Sequence[int]) -> list[PageIR]:
        """Parse the given 1-based pages into IR, in the order requested."""

    @abstractmethod
    def parse_file(self, path: Path, page_numbers: Sequence[int]) -> list[PageIR]:
        """Parse from a file path (the impl may mmap it to bound memory)."""
