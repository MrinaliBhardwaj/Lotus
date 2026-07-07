"""Celery application, wired to Redis with the §2.1 #8 hardening settings.

- acks_late + prefetch 1: a killed worker re-queues its task instead of losing
  it; safe because every pipeline stage is idempotent-by-checkpoint.
- time limits + max_memory_per_child: parsing untrusted PDFs is the largest
  attack surface — a decompression bomb kills one task process, not the fleet.
"""

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "lexa",
    broker=str(settings.redis_url),
    backend=str(settings.redis_url),
    include=["app.workers.tasks.ingestion"],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=settings.celery_task_time_limit,
    task_soft_time_limit=settings.celery_task_soft_time_limit,
    worker_max_memory_per_child=settings.celery_max_memory_per_child_kb,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)
