"""Guarded document-status transitions + job-state bookkeeping (CLAUDE.md §2.1 #8).

Every transition is ``UPDATE … WHERE status = expected`` — a stale or duplicate
transition (retried task, crashed-and-requeued worker) is a no-op instead of a
corruption. Async variants serve the API path; sync variants serve Celery
workers (§2.1 #7 — separate engines, same semantics).
"""

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import Document, DocumentStatus, IngestionJob, JobStage, JobState


async def transition_document(
    session: AsyncSession,
    document_id: uuid.UUID,
    expected: DocumentStatus,
    new: DocumentStatus,
    **extra_values: Any,
) -> bool:
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Document)
            .where(Document.id == document_id, Document.status == expected)
            .values(status=new, **extra_values)
        ),
    )
    return result.rowcount == 1


def transition_document_sync(
    session: Session,
    document_id: uuid.UUID,
    expected: DocumentStatus,
    new: DocumentStatus,
    **extra_values: Any,
) -> bool:
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Document)
            .where(Document.id == document_id, Document.status == expected)
            .values(status=new, **extra_values)
        ),
    )
    return result.rowcount == 1


def start_job_sync(session: Session, document_id: uuid.UUID, stage: JobStage) -> IngestionJob:
    """Upsert this stage's job row and mark it running.

    ``UNIQUE (document_id, stage)`` means a retried stage updates its row,
    never duplicates it; the retry counter records how many times it ran.
    """
    statement = pg_insert(IngestionJob).values(
        document_id=document_id,
        stage=stage,
        state=JobState.RUNNING,
        last_heartbeat=datetime.now(UTC),
    )
    statement = statement.on_conflict_do_update(
        index_elements=["document_id", "stage"],
        set_={
            "state": JobState.RUNNING,
            "last_heartbeat": datetime.now(UTC),
            "retry_count": IngestionJob.retry_count + 1,
            "error": None,
        },
    )
    session.execute(statement)
    session.commit()
    job = session.query(IngestionJob).filter_by(document_id=document_id, stage=stage).one()
    return job


def finish_job_sync(
    session: Session,
    job: IngestionJob,
    state: JobState,
    *,
    error: str | None = None,
    checkpoint: dict[str, Any] | None = None,
) -> None:
    job.state = state
    job.error = error
    if checkpoint is not None:
        job.checkpoint = checkpoint
    job.last_heartbeat = datetime.now(UTC)
    session.commit()


def fail_document_sync(
    session: Session, document_id: uuid.UUID, job: IngestionJob, error: str
) -> None:
    """Terminal failure: dead-letter the job and mark the document FAILED."""
    finish_job_sync(session, job, JobState.DEAD_LETTER, error=error)
    session.execute(
        update(Document).where(Document.id == document_id).values(status=DocumentStatus.FAILED)
    )
    session.commit()
