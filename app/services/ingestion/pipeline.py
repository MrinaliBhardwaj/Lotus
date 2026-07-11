"""Stage sequencing for the ingestion pipeline.

``enqueue_stage`` is the single seam between stage logic and Celery: services
and workers call it to hand off, and tests monkeypatch it to run stages inline
without a broker. Task names are dispatched as strings so the API process
never imports worker modules (and their heavy deps like PyMuPDF).
"""

import uuid

from app.models import JobStage

STAGE_TASK_NAMES: dict[JobStage, str] = {
    JobStage.VALIDATE: "lexa.validate_document",
    JobStage.PARSE: "lexa.parse_document",
    JobStage.STRUCTURE: "lexa.structure_document",
    JobStage.CHUNK: "lexa.chunk_document",
    JobStage.EMBED: "lexa.embed_document",
    JobStage.INDEX: "lexa.index_document",
}


def enqueue_stage(stage: JobStage, document_id: uuid.UUID) -> None:
    from app.workers.celery_app import celery_app  # deferred: keep API import-light

    celery_app.send_task(STAGE_TASK_NAMES[stage], args=[str(document_id)])


def enqueue_parse_batch(document_id: uuid.UUID, page_start: int, page_end: int) -> None:
    """Fan-out seam for the page-parallel parse stage."""
    from app.workers.celery_app import celery_app

    celery_app.send_task(
        "lexa.parse_page_batch", args=[str(document_id), page_start, page_end]
    )
