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
