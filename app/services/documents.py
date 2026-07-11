"""Document lifecycle: create → presigned upload → finalize → triage handoff.

Finalize (CLAUDE.md §2.1 #2) is the trust boundary for uploaded bytes. Order
matters and every check happens server-side, **before** any job is enqueued:
ownership → size (delete oversize objects) → magic bytes → SHA-256 →
per-user dedupe → guarded status flip → job row → enqueue.
"""

import hashlib
import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationFailedError
from app.models import Document, DocumentStatus, IngestionJob, JobStage
from app.services.ingestion import pipeline, status
from app.storage.base import ObjectStorage, document_key, key_owner

_HASH_RANGE_BYTES = 8 * 1024 * 1024
_PDF_MAGIC = b"%PDF-"  # per spec, must appear within the first 1024 bytes


async def create_document(
    session: AsyncSession,
    storage: ObjectStorage,
    settings: Settings,
    user_id: uuid.UUID,
    title: str,
) -> tuple[Document, str]:
    document = Document(user_id=user_id, title=title, s3_key="", status=DocumentStatus.UPLOADED)
    session.add(document)
    await session.flush()  # server-generated id needed for the tenant-prefixed key
    document.s3_key = document_key(user_id, document.id)
    await session.commit()
    upload_url = await storage.presign_upload(
        document.s3_key,
        content_type="application/pdf",
        expires_in=settings.s3_presign_expiry_seconds,
    )
    return document, upload_url


async def complete_document(
    session: AsyncSession,
    storage: ObjectStorage,
    settings: Settings,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
) -> IngestionJob:
    document = await get_owned_document(session, user_id, document_id)
    if document.status is not DocumentStatus.UPLOADED:
        raise ConflictError("document is already finalized")
    if key_owner(document.s3_key) != user_id:
        # cannot happen through the API (keys are server-built), so treat as an attack
        raise NotFoundError("document not found")

    info = await storage.head(document.s3_key)
    if info is None:
        raise ValidationFailedError("no uploaded file found — upload to the presigned URL first")
    if info.size_bytes > settings.max_upload_bytes:
        await storage.delete(document.s3_key)
        raise ValidationFailedError(
            f"file is {info.size_bytes} bytes; the limit is {settings.max_upload_bytes}"
        )

    head = await storage.get_range(document.s3_key, 0, 1023)
    if _PDF_MAGIC not in head:
        await storage.delete(document.s3_key)
        raise ValidationFailedError("file is not a PDF")

    doc_hash = await _sha256_of(storage, document.s3_key, info.size_bytes)
    s3_key = document.s3_key  # rollback() expires the instance; don't touch it after

    try:
        flipped = await status.transition_document(
            session,
            document.id,
            DocumentStatus.UPLOADED,
            DocumentStatus.VALIDATING,
            doc_hash=doc_hash,
            size_bytes=info.size_bytes,
            mime="application/pdf",
        )
        if not flipped:
            raise ConflictError("document is already finalized")
        job = IngestionJob(document_id=document.id, stage=JobStage.VALIDATE)
        session.add(job)
        await session.commit()
    except IntegrityError:
        # per-user dedupe (§2.1 #3): same bytes already ingested by THIS user
        await session.rollback()
        await storage.delete(s3_key)
        await session.execute(delete(Document).where(Document.id == document_id))
        await session.commit()
        raise ConflictError("an identical document already exists in your library") from None

    pipeline.enqueue_stage(JobStage.VALIDATE, document.id)
    return job


async def list_documents(session: AsyncSession, user_id: uuid.UUID) -> list[Document]:
    result = await session.scalars(
        select(Document)
        .where(Document.user_id == user_id, Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc())
    )
    return list(result)


async def get_document(
    session: AsyncSession, user_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    return await get_owned_document(session, user_id, document_id)


async def delete_document(
    session: AsyncSession, storage: ObjectStorage, user_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    """Soft delete. ``doc_hash`` is cleared so the unique slot frees up for a
    re-upload; artifacts are reaped later with the row (documented P1 gap)."""
    document = await get_owned_document(session, user_id, document_id)
    await session.execute(
        update(Document)
        .where(Document.id == document.id)
        .values(deleted_at=func.now(), doc_hash=None)
    )
    await session.commit()
    await storage.delete(document.s3_key)


async def get_owned_document(
    session: AsyncSession, user_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    """Tenant-scoped fetch (invariant 7). 404 for other tenants' documents —
    never 403, which would confirm existence."""
    document = await session.get(Document, document_id)
    if document is None or document.user_id != user_id or document.deleted_at is not None:
        raise NotFoundError("document not found")
    return document


async def _sha256_of(storage: ObjectStorage, key: str, size_bytes: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size_bytes:
        end = min(offset + _HASH_RANGE_BYTES, size_bytes) - 1
        digest.update(await storage.get_range(key, offset, end))
        offset = end + 1
    return digest.hexdigest()
