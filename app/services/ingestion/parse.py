"""Parse stage (sync — Celery workers): PDF → immutable IR artifacts.

Page-parallel: the stage task splits the document into page batches and fans
them out; each batch task parses its pages, writes one PageIR JSON artifact
per page, and checks off its batch in the job checkpoint under a row lock.
The batch that completes the set builds the canonical linearized text stream
(global char offsets — invariant 1's ``char_start``/``char_end`` come from
here) and hands off to section detection.

Everything is idempotent: re-running a batch overwrites the same artifacts,
and the checkpoint set ignores duplicates (§2.1 #8).
"""

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path

from app.core.config import Settings
from app.core.exceptions import ParserError
from app.models import Document, DocumentStatus, IngestionJob, JobStage, JobState
from app.parsers.base import PDFParser
from app.schemas.ir import LinearizedBlock, LinearizedText, PageIR
from app.services.ingestion import pipeline, status
from app.storage.base import ObjectStorage, artifact_key
from app.workers.session import worker_session

logger = logging.getLogger(__name__)

LINEARIZED_ARTIFACT = "ir/linearized.json"
BLOCK_SEPARATOR = "\n\n"


def page_artifact(page: int) -> str:
    return f"ir/page-{page:05d}.json"


def run_parse(document_id: uuid.UUID, *, settings: Settings) -> None:
    """Stage entry: initialize the checkpoint and fan out page batches."""
    with worker_session() as session:
        document = session.get(Document, document_id)
        if document is None or document.status is not DocumentStatus.PARSING:
            logger.info("parse(%s): stale or missing — no-op", document_id)
            return
        if document.page_count is None:
            status.fail_document_sync(
                session,
                document_id,
                status.start_job_sync(session, document_id, JobStage.PARSE),
                "document has no page count — validate stage did not run",
            )
            return
        status.start_job_sync(session, document_id, JobStage.PARSE)

        batch = settings.parse_batch_pages
        batches = [
            (start, min(start + batch - 1, document.page_count))
            for start in range(1, document.page_count + 1, batch)
        ]
        # Lock the job row before the read-modify-write so a redelivered
        # run_parse (acks_late) can't lose-update progress that in-flight
        # batches have already recorded — run_parse_batch takes the same
        # FOR UPDATE lock at fan-in. MERGE batches_done, never overwrite:
        # clobbering it back to a stale snapshot would force already-parsed
        # batches to re-download and re-parse the whole document.
        job = (
            session.query(IngestionJob)
            .filter_by(document_id=document_id, stage=JobStage.PARSE)
            .with_for_update()
            .one()
        )
        done = set(job.checkpoint.get("batches_done", []))  # resume after a crash
        job.checkpoint = {
            **job.checkpoint,
            "batches_total": len(batches),
            "batches_done": sorted(done),
        }
        session.commit()

    for start, end in batches:
        if f"{start}-{end}" not in done:
            pipeline.enqueue_parse_batch(document_id, start, end)


def run_parse_batch(
    document_id: uuid.UUID,
    page_start: int,
    page_end: int,
    *,
    settings: Settings,
    storage: ObjectStorage,
    parser: PDFParser,
) -> None:
    with worker_session() as session:
        document = session.get(Document, document_id)
        if document is None or document.status is not DocumentStatus.PARSING:
            logger.info("parse_batch(%s): stale or missing — no-op", document_id)
            return
        user_id, s3_key = document.user_id, document.s3_key

    # stream the PDF to a temp file and mmap it (H4 — no full in-memory copy);
    # a poison page that trips a ceiling fails the document instead of retrying
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp_path = Path(tmp.name)
        asyncio.run(storage.download_to_path(s3_key, tmp_path))
        try:
            pages = parser.parse_file(tmp_path, range(page_start, page_end + 1))
        except ParserError as exc:
            with worker_session() as session:
                job = status.start_job_sync(session, document_id, JobStage.PARSE)
                status.fail_document_sync(session, document_id, job, str(exc))
            return
    for page_ir in pages:
        asyncio.run(
            storage.put(
                artifact_key(user_id, document_id, page_artifact(page_ir.page)),
                page_ir.model_dump_json().encode(),
                content_type="application/json",
            )
        )

    with worker_session() as session:
        job = (
            session.query(IngestionJob)
            .filter_by(document_id=document_id, stage=JobStage.PARSE)
            .with_for_update()  # serialize checkpoint updates across batch workers
            .one()
        )
        done = set(job.checkpoint["batches_done"])
        done.add(f"{page_start}-{page_end}")
        job.checkpoint = {**job.checkpoint, "batches_done": sorted(done)}
        all_done = len(done) == job.checkpoint["batches_total"]
        session.commit()

    if not all_done:
        return

    with worker_session() as session:
        document = session.get(Document, document_id)
        assert document is not None
        _build_linearized(document.user_id, document_id, document.page_count or 0, storage)
        job = (
            session.query(IngestionJob)
            .filter_by(document_id=document_id, stage=JobStage.PARSE)
            .one()
        )
        status.finish_job_sync(session, job, JobState.SUCCEEDED, checkpoint=job.checkpoint)
        if status.transition_document_sync(
            session, document_id, DocumentStatus.PARSING, DocumentStatus.STRUCTURING
        ):
            session.commit()
            pipeline.enqueue_stage(JobStage.STRUCTURE, document_id)


def load_page_ir(
    user_id: uuid.UUID, document_id: uuid.UUID, page: int, storage: ObjectStorage
) -> PageIR:
    raw = asyncio.run(storage.get(artifact_key(user_id, document_id, page_artifact(page))))
    return PageIR.model_validate_json(raw)


def load_linearized(
    user_id: uuid.UUID, document_id: uuid.UUID, storage: ObjectStorage
) -> LinearizedText:
    raw = asyncio.run(storage.get(artifact_key(user_id, document_id, LINEARIZED_ARTIFACT)))
    return LinearizedText.model_validate_json(raw)


def _build_linearized(
    user_id: uuid.UUID, document_id: uuid.UUID, page_count: int, storage: ObjectStorage
) -> None:
    """Concatenate every block in global reading order into one canonical text
    stream, recording each block's char span. Chunk provenance (invariant 1)
    references these offsets forever after — they are never recomputed."""
    parts: list[str] = []
    blocks: list[LinearizedBlock] = []
    offset = 0
    for page in range(1, page_count + 1):
        page_ir = load_page_ir(user_id, document_id, page, storage)
        for block in page_ir.blocks:
            if not block.text:
                continue
            start = offset
            offset += len(block.text)
            parts.append(block.text)
            blocks.append(
                LinearizedBlock(id=block.id, page=page, char_start=start, char_end=offset)
            )
            offset += len(BLOCK_SEPARATOR)
            parts.append(BLOCK_SEPARATOR)
    text = "".join(parts[:-1]) if parts else ""  # no trailing separator
    linearized = LinearizedText(text=text, blocks=blocks)
    asyncio.run(
        storage.put(
            artifact_key(user_id, document_id, LINEARIZED_ARTIFACT),
            linearized.model_dump_json().encode(),
            content_type="application/json",
        )
    )
