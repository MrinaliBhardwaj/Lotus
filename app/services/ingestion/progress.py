"""SSE progress stream (§2.1 #9): DB snapshot FIRST, then Redis pub/sub deltas.

The snapshot-then-subscribe order means a client reconnecting after the last
event still learns the current state; after subscribing, the status is
re-read once so nothing slips through the snapshot→subscribe gap. If Redis
is unreachable the stream degrades to DB polling — live progress survives a
dead broker.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

import redis
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models import Document, DocumentStatus
from app.services.chats import sse_event
from app.services.ingestion import events

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {DocumentStatus.READY, DocumentStatus.FAILED, DocumentStatus.DEGRADED}


async def progress_stream(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    document_id: uuid.UUID,
    initial_status: DocumentStatus,
) -> AsyncIterator[str]:
    """Stream status deltas WITHOUT holding a DB connection for the stream's
    life (H1): the pub/sub wait touches no database, and each fresh status
    read opens and closes its own short-lived session."""
    last = initial_status
    yield sse_event(
        "status", {"document_id": str(document_id), "status": last.value, "snapshot": True}
    )
    if last in TERMINAL_STATUSES:
        return

    deadline = time.monotonic() + settings.progress_stream_timeout_seconds
    try:
        client = aioredis.from_url(  # type: ignore[no-untyped-call]
            str(settings.redis_url), socket_connect_timeout=1, socket_timeout=None
        )
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(events.status_channel(document_id))
            # close the snapshot→subscribe gap (one short-lived session)
            current = await _current_status(factory, document_id)
            if current is not None and current != last:
                last = current
                yield sse_event(
                    "status", {"document_id": str(document_id), "status": last.value}
                )
                if last in TERMINAL_STATUSES:
                    return
            while time.monotonic() < deadline:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
                if message is None:
                    yield ": keepalive\n\n"
                    continue
                payload = json.loads(message["data"])
                yield sse_event("status", payload)
                if payload.get("status") in {s.value for s in TERMINAL_STATUSES}:
                    return
        finally:
            await pubsub.aclose()
            await client.aclose()
    except (redis.RedisError, OSError):
        logger.warning("progress(%s): redis unavailable — falling back to DB polling", document_id)
        while time.monotonic() < deadline:
            await asyncio.sleep(settings.progress_poll_interval_seconds)
            current = await _current_status(factory, document_id)
            if current is None:
                return
            if current != last:
                last = current
                yield sse_event(
                    "status", {"document_id": str(document_id), "status": last.value}
                )
                if last in TERMINAL_STATUSES:
                    return


async def _current_status(
    factory: async_sessionmaker[AsyncSession], document_id: uuid.UUID
) -> DocumentStatus | None:
    # short-lived session: never pin a pooled connection across the stream (H1)
    async with factory() as session:
        value = await session.scalar(select(Document.status).where(Document.id == document_id))
    return DocumentStatus(value) if value is not None else None
