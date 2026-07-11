"""Ingestion pipeline tasks — thin Celery wrappers; stage logic lives in
``app/services/ingestion/`` so it is testable without a broker.

Stage tasks map 1:1 onto pipeline stages (validate → parse → structure →
chunk → embed → index) and are registered here as tasks land. ``ping`` exists
so the Celery wiring stays verifiable on its own.
"""

import uuid

from app.core.config import get_settings
from app.core.deps import get_storage
from app.workers.celery_app import celery_app


@celery_app.task(name="lexa.ping")
def ping() -> str:
    return "pong"


@celery_app.task(name="lexa.validate_document")
def validate_document(document_id: str) -> None:
    from app.services.ingestion.validate import run_validate

    run_validate(uuid.UUID(document_id), settings=get_settings(), storage=get_storage())


@celery_app.task(name="lexa.parse_document")
def parse_document(document_id: str) -> None:
    from app.services.ingestion.parse import run_parse

    run_parse(uuid.UUID(document_id), settings=get_settings())


@celery_app.task(name="lexa.parse_page_batch")
def parse_page_batch(document_id: str, page_start: int, page_end: int) -> None:
    from app.parsers.pymupdf_parser import PyMuPDFParser
    from app.services.ingestion.parse import run_parse_batch

    run_parse_batch(
        uuid.UUID(document_id),
        page_start,
        page_end,
        settings=get_settings(),
        storage=get_storage(),
        parser=PyMuPDFParser(),
    )


@celery_app.task(name="lexa.structure_document")
def structure_document(document_id: str) -> None:
    from app.services.ingestion.structure import run_structure

    run_structure(uuid.UUID(document_id), settings=get_settings(), storage=get_storage())


@celery_app.task(name="lexa.chunk_document")
def chunk_document(document_id: str) -> None:
    from app.services.ingestion.chunking import run_chunk

    run_chunk(uuid.UUID(document_id), settings=get_settings(), storage=get_storage())
