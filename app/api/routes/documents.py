"""Document endpoints — thin routers over services/documents.py (§3)."""

import uuid

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUserId, DbSession, SettingsDep, StorageDep, upload_rate_limit
from app.models import DocumentStatus
from app.schemas.documents import (
    DocumentCompleteResponse,
    DocumentCreateRequest,
    DocumentCreateResponse,
    DocumentOut,
)
from app.services import documents as documents_service

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post(
    "",
    response_model=DocumentCreateResponse,
    status_code=201,
    dependencies=[Depends(upload_rate_limit)],
)
async def create_document(
    body: DocumentCreateRequest,
    user_id: CurrentUserId,
    session: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
) -> DocumentCreateResponse:
    document, upload_url = await documents_service.create_document(
        session, storage, settings, user_id, body.title
    )
    return DocumentCreateResponse(
        id=document.id,
        title=document.title,
        upload_url=upload_url,
        upload_expires_in=settings.s3_presign_expiry_seconds,
        max_upload_bytes=settings.max_upload_bytes,
    )


@router.post(
    "/{document_id}/complete",
    response_model=DocumentCompleteResponse,
    status_code=202,
    dependencies=[Depends(upload_rate_limit)],
)
async def complete_document(
    document_id: uuid.UUID,
    user_id: CurrentUserId,
    session: DbSession,
    storage: StorageDep,
    settings: SettingsDep,
) -> DocumentCompleteResponse:
    job = await documents_service.complete_document(
        session, storage, settings, user_id, document_id
    )
    return DocumentCompleteResponse(
        document_id=job.document_id, job_id=job.id, status=DocumentStatus.VALIDATING
    )


@router.get("", response_model=list[DocumentOut])
async def list_documents(user_id: CurrentUserId, session: DbSession) -> list[DocumentOut]:
    documents = await documents_service.list_documents(session, user_id)
    return [DocumentOut.model_validate(document) for document in documents]


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: uuid.UUID, user_id: CurrentUserId, session: DbSession
) -> DocumentOut:
    document = await documents_service.get_document(session, user_id, document_id)
    return DocumentOut.model_validate(document)


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID, user_id: CurrentUserId, session: DbSession, storage: StorageDep
) -> None:
    await documents_service.delete_document(session, storage, user_id, document_id)
