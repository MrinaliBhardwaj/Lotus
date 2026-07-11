"""Task 7 gate tests: SSE progress — DB snapshot first, then live deltas
(§2.1 #9). Works over pub/sub when Redis is up, DB polling when it isn't."""

import json
import threading
import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy.orm import Session, sessionmaker

from app.models import DocumentStatus, JobStage
from app.services.ingestion.status import transition_document_sync
from tests.pdf_utils import make_pdf
from tests.test_documents import create_and_upload, register


def _parse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in body.strip().split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event is not None and data is not None:
            events.append((event, data))
    return events


async def test_progress_snapshot_then_delta(
    db_client: AsyncClient,
    sync_session_factory: sessionmaker[Session],
    captured_stages: list[tuple[JobStage, uuid.UUID]],
) -> None:
    headers = await register(db_client)
    document_id = await create_and_upload(db_client, headers, make_pdf(pages=1))
    completed = await db_client.post(f"/documents/{document_id}/complete", headers=headers)
    assert completed.status_code == 202  # stage captured, doc stays VALIDATING

    def _finish() -> None:  # worker-side flip; publishes the pub/sub delta itself
        with sync_session_factory() as session:
            assert transition_document_sync(
                session,
                uuid.UUID(document_id),
                DocumentStatus.VALIDATING,
                DocumentStatus.READY,
            )
            session.commit()

    # ASGITransport buffers the whole response, so the flip is armed to fire
    # ~1s after the stream opens; the server should emit the snapshot, then the
    # READY delta, then close (READY is terminal).
    timer = threading.Timer(1.0, _finish)
    timer.start()
    try:
        response = await db_client.get(f"/documents/{document_id}/progress", headers=headers)
    finally:
        timer.cancel()

    events = _parse_events(response.text)
    assert [kind for kind, _ in events] == ["status", "status"]
    snapshot, delta = events[0][1], events[1][1]
    assert (snapshot["status"], snapshot["snapshot"]) == ("VALIDATING", True)  # snapshot FIRST
    assert delta["status"] == "READY"  # then the live delta; stream closed after


async def test_progress_terminal_snapshot_closes_immediately(
    db_client: AsyncClient,
    sync_session_factory: sessionmaker[Session],
    captured_stages: list[tuple[JobStage, uuid.UUID]],
) -> None:
    headers = await register(db_client)
    document_id = await create_and_upload(db_client, headers, make_pdf(pages=1))
    await db_client.post(f"/documents/{document_id}/complete", headers=headers)

    with sync_session_factory() as session:
        assert transition_document_sync(
            session, uuid.UUID(document_id), DocumentStatus.VALIDATING, DocumentStatus.FAILED
        )
        session.commit()

    response = await db_client.get(f"/documents/{document_id}/progress", headers=headers)
    events = [line for line in response.text.split("\n") if line.startswith("data: ")]
    assert len(events) == 1
    assert json.loads(events[0][len("data: ") :])["status"] == "FAILED"


async def test_progress_is_tenant_scoped(
    db_client: AsyncClient, captured_stages: list[tuple[JobStage, uuid.UUID]]
) -> None:
    headers_a = await register(db_client)
    headers_b = await register(db_client)
    document_id = await create_and_upload(db_client, headers_a, make_pdf(pages=1))
    response = await db_client.get(f"/documents/{document_id}/progress", headers=headers_b)
    assert response.status_code == 404
