"""Shared DB seeding/loading helpers for worker-stage tests (sync path)."""

import asyncio
import uuid

from sqlalchemy.orm import Session, sessionmaker

from app.models import Document, DocumentStatus, IngestionJob, JobStage, User
from app.storage.base import document_key
from app.storage.local import LocalStorage


def seed_document(
    factory: sessionmaker[Session],
    storage: LocalStorage,
    data: bytes,
    *,
    status: DocumentStatus = DocumentStatus.VALIDATING,
    page_count: int | None = None,
) -> uuid.UUID:
    with factory() as session:
        user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_pw="x")
        session.add(user)
        session.flush()
        document = Document(
            user_id=user.id, title="T", s3_key="", status=status, page_count=page_count
        )
        session.add(document)
        session.flush()
        document.s3_key = document_key(user.id, document.id)
        session.commit()
        key = document.s3_key
        document_id = document.id
    asyncio.run(storage.put(key, data, content_type="application/pdf"))
    return document_id


def load_state(
    factory: sessionmaker[Session], document_id: uuid.UUID, stage: JobStage
) -> tuple[Document, IngestionJob | None]:
    with factory() as session:
        document = session.get(Document, document_id)
        assert document is not None
        job = (
            session.query(IngestionJob)
            .filter_by(document_id=document_id, stage=stage)
            .one_or_none()
        )
        return document, job
