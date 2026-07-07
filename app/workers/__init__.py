"""Celery workers. Entry point: ``celery -A app.workers worker -l info``."""

from app.workers.celery_app import celery_app

__all__ = ["celery_app"]
