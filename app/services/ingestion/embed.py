"""Embed + index stages (sync — Celery workers).

Embedding is deduped by ``content_hash``: each distinct hash is embedded once
and the vector fans out to every chunk carrying it (identical boilerplate
across a contract embeds once). Batches are written transactionally with
``embed_model``/``embed_version`` (selective re-embedding later), progress is
resumable (``embedding IS NULL`` is the checkpoint), and a token bucket keeps
requests under the provider's rate limit.

Only children get vectors — they are the retrieval unit; parents are context
expansion (DESIGN §4/§6). The document flips to READY only after every batch
has committed and the index stage's verification passes.
"""

import asyncio
import logging
import uuid

from sqlalchemy import func, text, update

from app.core import deps
from app.core.config import Settings
from app.core.ratelimit import SyncTokenBucket
from app.models import Chunk, ChunkType, Document, DocumentStatus, JobStage, JobState
from app.services.ingestion import pipeline, status
from app.storage.base import ObjectStorage
from app.workers.session import worker_session

logger = logging.getLogger(__name__)


def run_embed(document_id: uuid.UUID, *, settings: Settings, storage: ObjectStorage) -> None:
    provider = deps.get_embedding_provider(settings)
    bucket = SyncTokenBucket(settings.embed_requests_per_minute)
    batch_size = min(settings.embed_batch_size, provider.max_batch_size)

    with worker_session() as session:
        document = session.get(Document, document_id)
        if document is None or document.status is not DocumentStatus.EMBEDDING:
            logger.info("embed(%s): stale or missing — no-op", document_id)
            return
        job = status.start_job_sync(session, document_id, JobStage.EMBED)

        embedded = 0
        while True:
            # one row per distinct un-embedded content hash — global dedupe,
            # and crash-resumable: whatever committed stays done
            pending = (
                session.query(Chunk.content_hash, func.min(Chunk.content))
                .filter(
                    Chunk.document_id == document_id,
                    Chunk.chunk_type == ChunkType.CHILD,
                    Chunk.embedding.is_(None),
                )
                .group_by(Chunk.content_hash)
                .limit(batch_size)
                .all()
            )
            if not pending:
                break
            bucket.acquire()
            vectors = asyncio.run(provider.embed([content for _, content in pending]))
            for (content_hash, _), vector in zip(pending, vectors, strict=True):
                session.execute(
                    update(Chunk)
                    .where(
                        Chunk.document_id == document_id,
                        Chunk.chunk_type == ChunkType.CHILD,  # parents never get vectors
                        Chunk.content_hash == content_hash,
                        Chunk.embedding.is_(None),
                    )
                    .values(
                        embedding=vector,
                        embed_model=provider.model,
                        embed_version=provider.version,
                    )
                )
            embedded += len(pending)
            job.checkpoint = {**job.checkpoint, "hashes_embedded": embedded}
            session.commit()  # batch is transactional; a crash resumes here

        status.finish_job_sync(
            session, job, JobState.SUCCEEDED, checkpoint={"hashes_embedded": embedded}
        )
        if status.transition_document_sync(
            session, document_id, DocumentStatus.EMBEDDING, DocumentStatus.INDEXING
        ):
            session.commit()
            pipeline.enqueue_stage(JobStage.INDEX, document_id)


def run_index(document_id: uuid.UUID, *, settings: Settings) -> None:
    """HNSW/GIN indexes exist from the migration (partitioned indexes), so this
    stage is verification + planner stats: assert every child holds a vector,
    ANALYZE the partitions, and only then flip to READY."""
    with worker_session() as session:
        document = session.get(Document, document_id)
        if document is None or document.status is not DocumentStatus.INDEXING:
            logger.info("index(%s): stale or missing — no-op", document_id)
            return
        job = status.start_job_sync(session, document_id, JobStage.INDEX)

        missing = (
            session.query(func.count())
            .select_from(Chunk)
            .filter(
                Chunk.document_id == document_id,
                Chunk.chunk_type == ChunkType.CHILD,
                Chunk.embedding.is_(None),
            )
            .scalar()
        )
        total = (
            session.query(func.count())
            .select_from(Chunk)
            .filter(Chunk.document_id == document_id)
            .scalar()
        )
        if missing:
            status.fail_document_sync(
                session, document_id, job, f"{missing} chunks missing embeddings after embed stage"
            )
            return
        if not total:
            status.fail_document_sync(session, document_id, job, "document produced no chunks")
            return

        session.execute(text("ANALYZE chunks"))
        session.commit()
        status.finish_job_sync(session, job, JobState.SUCCEEDED, checkpoint={"chunks": total})
        if status.transition_document_sync(
            session, document_id, DocumentStatus.INDEXING, DocumentStatus.READY
        ):
            session.commit()
