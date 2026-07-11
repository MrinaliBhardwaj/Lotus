"""Document-status events over Redis pub/sub.

Every guarded status transition publishes a delta here; the SSE progress
endpoint subscribes (§2.1 #9: snapshot first, then these deltas). Publishing
is fail-open — a dead Redis degrades live progress, never the pipeline.
The payload carries the new status itself, so subscribers don't need to
re-read the DB on every event.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

import redis
import redis.asyncio as aioredis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_sync_client: redis.Redis | None = None


def status_channel(document_id: uuid.UUID) -> str:
    return f"lexa:doc-status:{document_id}"


def _payload(document_id: uuid.UUID, status_value: str) -> str:
    return json.dumps(
        {
            "document_id": str(document_id),
            "status": status_value,
            "ts": datetime.now(UTC).isoformat(),
        }
    )


def publish_status_sync(document_id: uuid.UUID, status_value: str) -> None:
    """Worker-side publish (sync client, §2.1 #7)."""
    global _sync_client
    try:
        if _sync_client is None:
            _sync_client = redis.Redis.from_url(
                str(get_settings().redis_url), socket_connect_timeout=1, socket_timeout=1
            )
        _sync_client.publish(status_channel(document_id), _payload(document_id, status_value))
    except (redis.RedisError, OSError):
        logger.debug("status event dropped (redis unavailable): %s", document_id)


async def publish_status_async(document_id: uuid.UUID, status_value: str) -> None:
    """API-side publish (finalize's UPLOADED→VALIDATING flip). A fresh client
    per call: connections are event-loop-bound, and this fires once per upload."""
    try:
        client = aioredis.from_url(  # type: ignore[no-untyped-call]
            str(get_settings().redis_url), socket_connect_timeout=1, socket_timeout=1
        )
        try:
            await client.publish(status_channel(document_id), _payload(document_id, status_value))
        finally:
            await client.aclose()
    except (redis.RedisError, OSError):
        logger.debug("status event dropped (redis unavailable): %s", document_id)
