"""Ingestion pipeline tasks.

Stage tasks are registered here from Task 3 onward, mapping 1:1 onto the
pipeline stages (validate/triage → parse → structure → chunk → embed → index).
`ping` exists so the Celery wiring is verifiable before any stage lands.
"""

from app.workers.celery_app import celery_app


@celery_app.task(name="lexa.ping")
def ping() -> str:
    return "pong"
