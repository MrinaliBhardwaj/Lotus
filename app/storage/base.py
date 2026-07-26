"""Storage interface + the tenant-prefixed key scheme (CLAUDE.md §2.1 #2).

Keys are always built through :func:`document_key` / :func:`artifact_key` so
every object lives under ``users/{user_id}/…`` — ownership of a key is
verifiable from the key itself at the upload-finalize step.
"""

import asyncio
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

# streaming reads/writes work in these chunks — bounds RSS on large objects
STREAM_CHUNK_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size_bytes: int
    content_type: str | None


@dataclass(frozen=True)
class PresignedUpload:
    """A direct-to-storage upload grant. S3 returns a POST with a policy that
    enforces a ``content-length-range`` server-side (H2 — a bare presigned PUT
    can't cap size); the local adapter returns a PUT to its own authenticated
    route. The client branches on ``method``."""

    url: str
    method: str  # "POST" (form fields + policy) or "PUT"
    fields: dict[str, str] = field(default_factory=dict)


def document_key(user_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """Key of the raw uploaded PDF."""
    return f"users/{user_id}/documents/{document_id}/raw.pdf"


def artifact_key(user_id: uuid.UUID, document_id: uuid.UUID, name: str) -> str:
    """Key of a derived artifact (IR JSON, page manifest, page images…)."""
    return f"users/{user_id}/documents/{document_id}/artifacts/{name}"


def key_owner(key: str) -> uuid.UUID | None:
    """Extract the owning user_id from a tenant-prefixed key, or None if malformed."""
    parts = key.split("/")
    if len(parts) < 2 or parts[0] != "users":
        return None
    try:
        return uuid.UUID(parts[1])
    except ValueError:
        return None


class ObjectStorage(ABC):
    """Async object-storage interface. Implementations must be side-effect-free
    beyond the addressed key (idempotent puts/deletes) so pipeline stages can retry."""

    @abstractmethod
    async def presign_upload(
        self, key: str, *, content_type: str, max_bytes: int, expires_in: int
    ) -> PresignedUpload:
        """Grant a direct upload capped at ``max_bytes`` server-side."""

    @abstractmethod
    async def presign_download(self, key: str, *, expires_in: int) -> str: ...

    @abstractmethod
    async def put(self, key: str, data: bytes, *, content_type: str) -> None: ...

    @abstractmethod
    async def get(self, key: str) -> bytes: ...

    @abstractmethod
    async def get_range(self, key: str, start: int, end: int) -> bytes:
        """Inclusive byte range — used to magic-byte-check heads without full downloads."""

    @abstractmethod
    def stream(self, key: str, *, chunk_size: int = STREAM_CHUNK_BYTES) -> AsyncIterator[bytes]:
        """Yield the object in chunks over a SINGLE client/handle — hashing a
        500 MB upload this way avoids both a full in-memory copy and the
        per-range client churn of repeated ``get_range`` calls (L1)."""

    async def download_to_path(self, key: str, dest: Path) -> None:
        """Stream the object to a local file (H4 — lets the parser mmap the PDF
        via ``fitz.open(path)`` instead of holding the whole thing in RAM)."""
        handle = await asyncio.to_thread(open, dest, "wb")
        try:
            async for chunk in self.stream(key):
                await asyncio.to_thread(handle.write, chunk)
        finally:
            await asyncio.to_thread(handle.close)

    @abstractmethod
    async def head(self, key: str) -> ObjectInfo | None:
        """Metadata for a key, or None if it does not exist."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete the object; deleting a missing key is a no-op."""
