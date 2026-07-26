"""Filesystem adapter for tests and keyless local dev.

"Presigned" upload URLs point at the API's ``PUT /local-uploads/{key}`` route
(which enforces auth + key ownership), so the browser-side upload flow is the
same shape as with real S3/MinIO presigning.
"""

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

from app.core.exceptions import StorageError
from app.storage.base import STREAM_CHUNK_BYTES, ObjectInfo, ObjectStorage, PresignedUpload


class LocalStorage(ObjectStorage):
    def __init__(self, root: str, public_base_url: str = "") -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._public_base_url = public_base_url.rstrip("/")

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise StorageError(f"key escapes storage root: {key}")
        return path

    def _meta_path(self, key: str) -> Path:
        return self._path(key + ".meta.json")

    async def presign_upload(
        self, key: str, *, content_type: str, max_bytes: int, expires_in: int
    ) -> PresignedUpload:
        self._path(key)  # validate
        # PUT to the authenticated local route, which enforces max_bytes itself
        return PresignedUpload(url=f"{self._public_base_url}/local-uploads/{key}", method="PUT")

    async def presign_download(self, key: str, *, expires_in: int) -> str:
        self._path(key)
        return f"{self._public_base_url}/local-uploads/{key}"

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        def _write() -> None:
            path = self._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self._meta_path(key).write_text(json.dumps({"content_type": content_type}))

        await asyncio.to_thread(_write)

    async def get(self, key: str) -> bytes:
        def _read() -> bytes:
            path = self._path(key)
            if not path.is_file():
                raise StorageError(f"object not found: {key}")
            return path.read_bytes()

        return await asyncio.to_thread(_read)

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        def _read_range() -> bytes:
            path = self._path(key)
            if not path.is_file():
                raise StorageError(f"object not found: {key}")
            with path.open("rb") as handle:
                handle.seek(start)
                return handle.read(end - start + 1)

        return await asyncio.to_thread(_read_range)

    async def stream(
        self, key: str, *, chunk_size: int = STREAM_CHUNK_BYTES
    ) -> AsyncIterator[bytes]:
        path = self._path(key)
        if not await asyncio.to_thread(path.is_file):
            raise StorageError(f"object not found: {key}")
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            while chunk := await asyncio.to_thread(handle.read, chunk_size):
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    async def head(self, key: str) -> ObjectInfo | None:
        def _stat() -> ObjectInfo | None:
            path = self._path(key)
            if not path.is_file():
                return None
            content_type = None
            meta = self._meta_path(key)
            if meta.is_file():
                content_type = json.loads(meta.read_text()).get("content_type")
            return ObjectInfo(key=key, size_bytes=path.stat().st_size, content_type=content_type)

        return await asyncio.to_thread(_stat)

    async def delete(self, key: str) -> None:
        def _delete() -> None:
            self._path(key).unlink(missing_ok=True)
            self._meta_path(key).unlink(missing_ok=True)

        await asyncio.to_thread(_delete)

    def wipe(self) -> None:
        """Test helper — remove everything under the storage root."""
        shutil.rmtree(self._root, ignore_errors=True)
        self._root.mkdir(parents=True, exist_ok=True)
