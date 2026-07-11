"""Upload target for the local storage adapter (dev/tests only).

With real S3/MinIO the browser PUTs straight to object storage and this route
is never used — it 404s unless the configured backend is local. It mirrors
what a presigned URL grants: authenticated caller, own-tenant key only, size
ceiling enforced while streaming (this path is exempt from the JSON body-size
middleware and applies the upload ceiling instead).
"""

from fastapi import APIRouter, Request, Response

from app.api.deps import CurrentUserId, SettingsDep, StorageDep
from app.core.exceptions import (
    NotFoundError,
    PermissionDeniedError,
    StorageError,
    ValidationFailedError,
)
from app.storage.base import key_owner
from app.storage.local import LocalStorage

router = APIRouter(prefix="/local-uploads", tags=["local-uploads"])


@router.put("/{key:path}", status_code=200)
async def upload_local(
    key: str,
    request: Request,
    user_id: CurrentUserId,
    storage: StorageDep,
    settings: SettingsDep,
) -> dict[str, int]:
    if not isinstance(storage, LocalStorage):
        raise NotFoundError("local uploads are disabled with this storage backend")
    if key_owner(key) != user_id:
        raise PermissionDeniedError("key does not belong to the authenticated user")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.max_upload_bytes:
            raise ValidationFailedError("upload exceeds the size limit")
    await storage.put(key, bytes(body), content_type="application/pdf")
    return {"size": len(body)}


@router.get("/{key:path}")
async def download_local(key: str, user_id: CurrentUserId, storage: StorageDep) -> Response:
    """Download counterpart of the presigned-GET URL the local adapter mints
    (the PDF.js viewer fetches this). Same guarantees: auth + own-tenant key."""
    if not isinstance(storage, LocalStorage):
        raise NotFoundError("local downloads are disabled with this storage backend")
    if key_owner(key) != user_id:
        raise PermissionDeniedError("key does not belong to the authenticated user")
    try:
        data = await storage.get(key)
    except StorageError:
        raise NotFoundError("object not found") from None
    return Response(content=data, media_type="application/pdf")
