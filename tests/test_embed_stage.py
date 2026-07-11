"""Task 6 gate tests: embedding + indexing → a document reaches READY and both
search access paths (vector + lexical) return rows."""

import hashlib
import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models import Chunk, ChunkType, DocumentStatus, JobStage, JobState
from app.providers.embeddings.fake import FakeEmbeddingProvider
from app.services.ingestion.embed import run_embed, run_index
from app.services.ingestion.validate import run_validate
from app.storage.local import LocalStorage
from tests.db_utils import load_state, seed_document
from tests.pdf_utils import make_contract_pdf


def _insert_child(session: Session, document_id: uuid.UUID, content: str) -> uuid.UUID:
    chunk = Chunk(
        id=uuid.uuid4(),
        document_id=document_id,
        chunk_type=ChunkType.CHILD,
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        page_start=1,
        page_end=1,
        bboxes=[{"page": 1, "rect": [0.1, 0.1, 0.9, 0.2]}],
        char_start=0,
        char_end=len(content),
        section_path="1",
        section_title="One",
        block_ids=["b-1-0"],
        token_count=len(content.split()),
    )
    session.add(chunk)
    return chunk.id


class CountingProvider(FakeEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__(1536)
        self.batches: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return await super().embed(texts)


def test_full_pipeline_reaches_ready_and_both_search_paths_work(
    sync_session_factory: sessionmaker[Session],
    db_settings: Settings,
    inline_pipeline: list[JobStage],
) -> None:
    storage = LocalStorage(db_settings.local_storage_path)
    document_id = seed_document(sync_session_factory, storage, make_contract_pdf())
    run_validate(document_id, settings=db_settings, storage=storage)

    assert inline_pipeline == [
        JobStage.PARSE,
        JobStage.STRUCTURE,
        JobStage.CHUNK,
        JobStage.EMBED,
        JobStage.INDEX,
    ]
    document, index_job = load_state(sync_session_factory, document_id, JobStage.INDEX)
    assert document.status is DocumentStatus.READY  # the gate
    assert index_job is not None and index_job.state is JobState.SUCCEEDED

    with sync_session_factory() as session:
        chunks = session.scalars(
            select(Chunk).where(Chunk.document_id == document_id)
        ).all()
        children = [c for c in chunks if c.chunk_type is ChunkType.CHILD]
        parents = [c for c in chunks if c.chunk_type is ChunkType.PARENT]
        assert children and parents
        for child in children:
            assert child.embedding is not None
            assert child.embed_model == "fake-embedding-v1"
            assert child.embed_version == "1"
        assert all(p.embedding is None for p in parents)  # children are the retrieval unit

        # vector access path
        nearest = session.execute(
            text(
                "SELECT id FROM chunks WHERE document_id = :d AND embedding IS NOT NULL "
                "ORDER BY embedding <=> (SELECT embedding FROM chunks "
                "WHERE document_id = :d AND embedding IS NOT NULL LIMIT 1) LIMIT 3"
            ),
            {"d": document_id},
        ).all()
        assert len(nearest) == 3
        # lexical access path (both configs, each queried with ITS config)
        for column, config in (("tsv_english", "english"), ("tsv_simple", "simple")):
            hits = session.execute(
                text(
                    f"SELECT count(*) FROM chunks WHERE document_id = :d "  # noqa: S608
                    f"AND {column} @@ plainto_tsquery('{config}', 'obligations')"
                ),
                {"d": document_id},
            ).scalar()
            assert hits is not None and hits > 0


def test_embed_dedupes_by_content_hash(
    sync_session_factory: sessionmaker[Session],
    db_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    captured_stages: list[tuple[JobStage, uuid.UUID]],
) -> None:
    storage = LocalStorage(db_settings.local_storage_path)
    document_id = seed_document(
        sync_session_factory, storage, b"%PDF-x", status=DocumentStatus.EMBEDDING
    )
    with sync_session_factory() as session:
        a = _insert_child(session, document_id, "identical boilerplate clause")
        b = _insert_child(session, document_id, "identical boilerplate clause")
        c = _insert_child(session, document_id, "a completely different clause")
        session.commit()

    provider = CountingProvider()
    monkeypatch.setattr("app.core.deps.get_embedding_provider", lambda s=None: provider)
    run_embed(document_id, settings=db_settings, storage=storage)

    embedded_texts = [t for batch in provider.batches for t in batch]
    assert sorted(embedded_texts) == [
        "a completely different clause",
        "identical boilerplate clause",  # once, not twice
    ]
    with sync_session_factory() as session:
        va, vb, vc = (
            session.get(Chunk, (cid, document_id)).embedding  # type: ignore[union-attr]
            for cid in (a, b, c)
        )
        assert list(va) == list(vb)  # fanned out to both carriers
        assert list(va) != list(vc)
    assert captured_stages == [(JobStage.INDEX, document_id)]


def test_embed_resumes_skipping_already_embedded(
    sync_session_factory: sessionmaker[Session],
    db_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    captured_stages: list[tuple[JobStage, uuid.UUID]],
) -> None:
    storage = LocalStorage(db_settings.local_storage_path)
    document_id = seed_document(
        sync_session_factory, storage, b"%PDF-x", status=DocumentStatus.EMBEDDING
    )
    with sync_session_factory() as session:
        done = _insert_child(session, document_id, "already embedded before the crash")
        _insert_child(session, document_id, "still pending")
        session.commit()
        chunk = session.get(Chunk, (done, document_id))
        assert chunk is not None
        chunk.embedding = [0.5] * 1536
        chunk.embed_model = "fake-embedding-v1"
        chunk.embed_version = "1"
        session.commit()

    provider = CountingProvider()
    monkeypatch.setattr("app.core.deps.get_embedding_provider", lambda s=None: provider)
    run_embed(document_id, settings=db_settings, storage=storage)

    assert [t for batch in provider.batches for t in batch] == ["still pending"]


def test_index_dead_letters_on_missing_embeddings(
    sync_session_factory: sessionmaker[Session],
    db_settings: Settings,
) -> None:
    storage = LocalStorage(db_settings.local_storage_path)
    document_id = seed_document(
        sync_session_factory, storage, b"%PDF-x", status=DocumentStatus.INDEXING
    )
    with sync_session_factory() as session:
        _insert_child(session, document_id, "never got a vector")
        session.commit()

    run_index(document_id, settings=db_settings)
    document, job = load_state(sync_session_factory, document_id, JobStage.INDEX)
    assert document.status is DocumentStatus.FAILED
    assert job is not None and job.state is JobState.DEAD_LETTER
    assert job.error is not None and "missing embeddings" in job.error
