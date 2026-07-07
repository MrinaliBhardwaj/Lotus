import uuid

import pytest

from app.core.exceptions import StorageError
from app.storage.base import artifact_key, document_key, key_owner
from app.storage.local import LocalStorage


async def test_put_get_head_delete(local_storage: LocalStorage) -> None:
    await local_storage.put("users/u1/doc.pdf", b"%PDF-1.7 data", content_type="application/pdf")

    assert await local_storage.get("users/u1/doc.pdf") == b"%PDF-1.7 data"

    info = await local_storage.head("users/u1/doc.pdf")
    assert info is not None
    assert info.size_bytes == len(b"%PDF-1.7 data")
    assert info.content_type == "application/pdf"

    await local_storage.delete("users/u1/doc.pdf")
    assert await local_storage.head("users/u1/doc.pdf") is None


async def test_get_range_reads_magic_bytes(local_storage: LocalStorage) -> None:
    await local_storage.put("k", b"%PDF-1.7 rest", content_type="application/pdf")
    assert await local_storage.get_range("k", 0, 4) == b"%PDF-"


async def test_head_missing_returns_none(local_storage: LocalStorage) -> None:
    assert await local_storage.head("missing") is None


async def test_path_traversal_rejected(local_storage: LocalStorage) -> None:
    with pytest.raises(StorageError):
        await local_storage.put("../escape", b"x", content_type="text/plain")


def test_tenant_prefixed_key_scheme() -> None:
    user = uuid.uuid4()
    doc = uuid.uuid4()
    raw = document_key(user, doc)
    art = artifact_key(user, doc, "manifest.json")
    assert raw.startswith(f"users/{user}/")
    assert art.startswith(f"users/{user}/")
    # ownership is verifiable from the key itself (§2.1 #2)
    assert key_owner(raw) == user
    assert key_owner("garbage/key") is None
    assert key_owner("users/not-a-uuid/x") is None
