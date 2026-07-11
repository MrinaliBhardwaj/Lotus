"""Task 7 gate tests: hybrid retrieval, RRF, citation validation, and the full
ask-a-question flow — streamed answer citing a page, fabricated IDs stripped."""

import asyncio
import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.models import ChunkType, DocumentStatus, JobStage
from app.services.generation.answer import CitationStreamFilter
from app.services.ingestion.validate import run_validate
from app.services.retrieval.hybrid import retrieve_sources, rrf_fuse
from app.storage.local import LocalStorage
from tests.db_utils import seed_document
from tests.pdf_utils import make_contract_pdf
from tests.test_documents import create_and_upload, register

# --- RRF ----------------------------------------------------------------------


def test_rrf_fuses_ranks_only() -> None:
    a, b, c, d = (uuid.UUID(int=i) for i in range(1, 5))
    fused = rrf_fuse([[a, b, c], [c, a, d]], k=60)
    # a: 1/61+1/62 > c: 1/61+1/63 > b: 1/62 > d: 1/63
    assert fused == [a, c, b, d]


def test_rrf_single_list_preserves_order() -> None:
    a, b = uuid.UUID(int=1), uuid.UUID(int=2)
    assert rrf_fuse([[a, b]]) == [a, b]


# --- streaming citation validation (invariant 5) --------------------------------


def test_citation_filter_strips_fabricated_ids_split_across_chunks() -> None:
    stream_filter = CitationStreamFilter(valid_sids={1, 2})
    out = stream_filter.feed("grounded [S1] then fabricated [S")
    out += stream_filter.feed("99] then [S2] and [S")
    out += stream_filter.feed("3]")
    out += stream_filter.flush()
    assert out == "grounded [S1] then fabricated  then [S2] and "
    assert stream_filter.used_sids == {1, 2}


def test_citation_filter_passes_plain_brackets() -> None:
    stream_filter = CitationStreamFilter(valid_sids=set())
    assert stream_filter.feed("array[0] and [note] stay") + stream_filter.flush() == (
        "array[0] and [note] stay"
    )


# --- hybrid retrieval -----------------------------------------------------------


async def _ingest(
    sync_session_factory: sessionmaker[Session], db_settings: Settings
) -> tuple[uuid.UUID, uuid.UUID]:
    """Returns (owner_user_id, document_id) of a READY contract document."""
    storage = LocalStorage(db_settings.local_storage_path)
    document_id = await asyncio.to_thread(
        seed_document, sync_session_factory, storage, make_contract_pdf()
    )
    await asyncio.to_thread(
        run_validate, document_id, settings=db_settings, storage=storage
    )
    with sync_session_factory() as session:
        from app.models import Document

        document = session.get(Document, document_id)
        assert document is not None and document.status is DocumentStatus.READY
        return document.user_id, document_id


async def test_retrieve_sources_hybrid(
    async_db_session: AsyncSession,
    sync_session_factory: sessionmaker[Session],
    db_settings: Settings,
    inline_pipeline: list[JobStage],
) -> None:
    owner_id, document_id = await _ingest(sync_session_factory, db_settings)
    sources = await retrieve_sources(
        async_db_session, db_settings, owner_id, document_id, "obligations survive termination"
    )
    assert sources
    assert [s.sid for s in sources] == list(range(1, len(sources) + 1))
    for source in sources:
        assert source.chunk.document_id == document_id  # partition-scoped (invariant 8)
        assert source.chunk.chunk_type is ChunkType.PARENT  # child→parent expansion
        assert source.chunk.page_start >= 1


async def test_retrieval_is_tenant_scoped(
    async_db_session: AsyncSession,
    sync_session_factory: sessionmaker[Session],
    db_settings: Settings,
    inline_pipeline: list[JobStage],
) -> None:
    _, document_id = await _ingest(sync_session_factory, db_settings)
    with pytest.raises(NotFoundError):  # 404-shaped, not 403 (invariant 7)
        await retrieve_sources(
            async_db_session, db_settings, uuid.uuid4(), document_id, "anything"
        )


# --- the full ask flow over the API (the Task 7 gate) ----------------------------


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    parsed = []
    for block in body.strip().split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event is not None and data is not None:
            parsed.append((event, data))
    return parsed


async def test_ask_question_streams_cited_answer(
    db_client: AsyncClient,
    sync_session_factory: sessionmaker[Session],
    db_settings: Settings,
    inline_pipeline: list[JobStage],
) -> None:
    headers = await register(db_client)
    document_id = await create_and_upload(db_client, headers, make_contract_pdf())
    # inline pipeline drives the whole ingestion inside the finalize request
    completed = await db_client.post(f"/documents/{document_id}/complete", headers=headers)
    assert completed.status_code == 202
    detail = await db_client.get(f"/documents/{document_id}", headers=headers)
    assert detail.json()["status"] == "READY"

    chat = await db_client.post("/chats", json={"document_id": document_id}, headers=headers)
    assert chat.status_code == 201, chat.text
    chat_id = chat.json()["id"]

    response = await db_client.post(
        f"/chats/{chat_id}/messages",
        json={"content": "What obligations survive termination?"},
        headers=headers,
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "sources" and kinds[-1] == "done" and "delta" in kinds

    sources = events[0][1]["sources"]
    assert sources and all("page_start" in s for s in sources)

    answer = "".join(data["text"] for kind, data in events if kind == "delta")
    assert "[S1]" in answer  # grounded citation kept
    assert "[S99]" not in answer  # fabricated citation structurally stripped

    done = events[-1][1]
    citations = done["citations"]
    assert citations and citations[0]["sid"] == 1
    assert citations[0]["page_start"] >= 1  # page resolved server-side, not by the model
    assert all(c["sid"] != 99 for c in citations)

    # both messages persisted; assistant carries the resolved citations
    history = await db_client.get(f"/chats/{chat_id}/messages", headers=headers)
    roles = [m["role"] for m in history.json()]
    assert roles == ["user", "assistant"]
    assert history.json()[1]["citations"] == citations


async def test_chat_is_tenant_scoped(
    db_client: AsyncClient,
    sync_session_factory: sessionmaker[Session],
    db_settings: Settings,
    inline_pipeline: list[JobStage],
) -> None:
    headers_a = await register(db_client)
    document_id = await create_and_upload(db_client, headers_a, make_contract_pdf(2, 3))
    await db_client.post(f"/documents/{document_id}/complete", headers=headers_a)
    chat = await db_client.post("/chats", json={"document_id": document_id}, headers=headers_a)
    chat_id = chat.json()["id"]

    headers_b = await register(db_client)
    assert (
        await db_client.post("/chats", json={"document_id": document_id}, headers=headers_b)
    ).status_code == 404
    assert (
        await db_client.post(
            f"/chats/{chat_id}/messages", json={"content": "hi"}, headers=headers_b
        )
    ).status_code == 404
    assert (
        await db_client.get(f"/chats/{chat_id}/messages", headers=headers_b)
    ).status_code == 404


async def test_chat_requires_ready_document(
    db_client: AsyncClient, captured_stages: list[tuple[JobStage, uuid.UUID]]
) -> None:
    headers = await register(db_client)
    created = await db_client.post("/documents", json={"title": "T"}, headers=headers)
    response = await db_client.post(
        "/chats", json={"document_id": created.json()["id"]}, headers=headers
    )
    assert response.status_code == 422
    assert "not ready" in response.json()["detail"]
