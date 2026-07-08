"""Task 2 gate tests: the Phase-1 schema, its invariants, and the migration chain.

These use the ORM models against the migrated database, which doubles as a
DDL ↔ ORM parity check.
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    CHUNK_PARTITION_COUNT,
    EMBEDDING_DIMENSIONS,
    Chunk,
    ChunkType,
    Document,
    DocumentStatus,
    IngestionJob,
    JobStage,
    Section,
    User,
)
from tests.conftest import run_alembic

EXPECTED_TABLES = {
    "users",
    "documents",
    "sections",
    "chunks",
    "ingestion_jobs",
    "chats",
    "messages",
}


@pytest.fixture(scope="session")
def engine(migrated_db_url: str) -> Iterator[Engine]:
    sync_url = migrated_db_url.replace("+asyncpg", "+psycopg")
    engine = create_engine(sync_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def _make_user(db: Session) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_pw="x")
    db.add(user)
    db.flush()
    return user


def _make_document(db: Session, user: User, doc_hash: str | None = None) -> Document:
    document = Document(
        user_id=user.id,
        title="Test Contract",
        s3_key=f"users/{user.id}/documents/{uuid.uuid4()}/raw.pdf",
        status=DocumentStatus.UPLOADED,
        doc_hash=doc_hash,
    )
    db.add(document)
    db.flush()
    return document


def _make_chunk(db: Session, document: Document, **overrides: object) -> Chunk:
    fields: dict[str, object] = {
        "document_id": document.id,
        "chunk_type": ChunkType.CHILD,
        "content": "The liability cap is $5,000,000 per clause 12.3.1.",
        "content_hash": uuid.uuid4().hex,
        "page_start": 14,
        "page_end": 14,
        "bboxes": [{"page": 14, "rect": [0.1, 0.2, 0.9, 0.25]}],
        "char_start": 1000,
        "char_end": 1050,
        "section_path": "12 > 12.3 > 12.3.1",
        "section_title": "Limitation of Liability",
        "block_ids": ["b-14-3"],
        "token_count": 17,
    }
    fields.update(overrides)
    chunk = Chunk(**fields)
    db.add(chunk)
    db.flush()
    return chunk


# --- structure -----------------------------------------------------------------


def test_all_phase1_tables_exist(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert tables >= EXPECTED_TABLES


def test_chunks_has_16_hash_partitions(db: Session) -> None:
    count = db.scalar(
        text("SELECT count(*) FROM pg_inherits WHERE inhparent = 'chunks'::regclass")
    )
    assert count == CHUNK_PARTITION_COUNT
    strategy = db.scalar(
        text(
            "SELECT partstrat FROM pg_partitioned_table "
            "WHERE partrelid = 'chunks'::regclass"
        )
    )
    assert strategy == "h"  # hash


def test_partitioned_indexes_hnsw_and_gin(db: Session) -> None:
    rows = db.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'chunks'")
    ).all()
    defs = dict(rows.tuples()) if hasattr(rows, "tuples") else dict(rows)
    assert "USING hnsw (embedding vector_cosine_ops)" in defs["ix_chunks_embedding_hnsw"]
    assert "USING gin (tsv_english)" in defs["ix_chunks_tsv_english"]
    assert "USING gin (tsv_simple)" in defs["ix_chunks_tsv_simple"]


def test_composite_pk_includes_partition_key(engine: Engine) -> None:
    pk = inspect(engine).get_pk_constraint("chunks")
    assert pk["constrained_columns"] == ["id", "document_id"]


# --- data invariants ------------------------------------------------------------


def test_chunk_roundtrip_with_full_provenance(db: Session) -> None:
    user = _make_user(db)
    document = _make_document(db, user)
    parent = _make_chunk(db, document, chunk_type=ChunkType.PARENT, token_count=900)
    child = _make_chunk(
        db,
        document,
        parent_chunk_id=parent.id,
        embedding=[0.01] * EMBEDDING_DIMENSIONS,
        embed_model="text-embedding-3-small",
        embed_version="1",
    )
    db.commit()

    fetched = db.execute(
        select(Chunk).where(Chunk.id == child.id, Chunk.document_id == document.id)
    ).scalar_one()
    # every provenance field present (invariant 1) — normalized bboxes list
    assert fetched.bboxes == [{"page": 14, "rect": [0.1, 0.2, 0.9, 0.25]}]
    assert (fetched.page_start, fetched.page_end) == (14, 14)
    assert (fetched.char_start, fetched.char_end) == (1000, 1050)
    assert fetched.section_path == "12 > 12.3 > 12.3.1"
    assert fetched.parent_chunk_id == parent.id
    assert len(fetched.embedding) == EMBEDDING_DIMENSIONS


def test_tsvector_generated_columns_populate(db: Session) -> None:
    user = _make_user(db)
    document = _make_document(db, user)
    chunk = _make_chunk(db, document, content="Indemnification obligations survive termination")
    db.commit()
    english, simple = db.execute(
        text("SELECT tsv_english::text, tsv_simple::text FROM chunks WHERE id = :id"),
        {"id": chunk.id},
    ).one()
    assert "indemnif" in english  # stemmed
    assert "indemnification" in simple  # exact — defined terms not stemmed (§2.1 #10)


def test_lexical_and_vector_queries_execute(db: Session) -> None:
    # the two retrieval access paths (Task 7) are exercisable at the SQL level
    hits = db.execute(
        text(
            "SELECT count(*) FROM chunks "
            "WHERE tsv_english @@ plainto_tsquery('english', 'liability')"
        )
    ).scalar()
    assert hits is not None
    nearest = db.execute(
        text(
            "SELECT id FROM chunks WHERE embedding IS NOT NULL ORDER BY embedding <=> "
            "(SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1) LIMIT 1"
        )
    ).all()
    assert isinstance(nearest, list)


def test_dedupe_is_per_user_not_corpus_wide(db: Session) -> None:
    doc_hash = uuid.uuid4().hex[:64]
    user_a = _make_user(db)
    user_b = _make_user(db)
    _make_document(db, user_a, doc_hash=doc_hash)
    # same content, different tenant: allowed (§2.1 #3 — no cross-tenant side channel)
    _make_document(db, user_b, doc_hash=doc_hash)
    db.commit()
    # same tenant, same hash: rejected
    with pytest.raises(IntegrityError):
        _make_document(db, user_a, doc_hash=doc_hash)
    db.rollback()


def test_ingestion_job_unique_per_document_stage(db: Session) -> None:
    user = _make_user(db)
    document = _make_document(db, user)
    db.add(IngestionJob(document_id=document.id, stage=JobStage.VALIDATE))
    db.flush()
    with pytest.raises(IntegrityError):
        db.add(IngestionJob(document_id=document.id, stage=JobStage.VALIDATE))
        db.flush()
    db.rollback()


def test_empty_bboxes_rejected(db: Session) -> None:
    user = _make_user(db)
    document = _make_document(db, user)
    with pytest.raises(IntegrityError):
        _make_chunk(db, document, bboxes=[])
    db.rollback()


def test_document_delete_cascades(db: Session) -> None:
    user = _make_user(db)
    document = _make_document(db, user)
    _make_chunk(db, document)
    db.add(
        Section(
            document_id=document.id, level=1, title="Art. 1", section_path="1", page_start=1
        )
    )
    db.add(IngestionJob(document_id=document.id, stage=JobStage.PARSE))
    db.commit()

    db.delete(db.get(Document, document.id))
    db.commit()

    for table in ("chunks", "sections", "ingestion_jobs"):
        remaining = db.scalar(
            text(f"SELECT count(*) FROM {table} WHERE document_id = :id"),  # noqa: S608
            {"id": document.id},
        )
        assert remaining == 0, table


# --- migration chain (runs last: rebuilds the schema) -----------------------------


def test_zz_migrations_downgrade_and_upgrade_cleanly(
    migrated_db_url: str, engine: Engine
) -> None:
    engine.dispose()  # drop pooled connections before DDL churn
    run_alembic(["downgrade", "base"], migrated_db_url)
    run_alembic(["upgrade", "head"], migrated_db_url)
    tables = set(inspect(engine).get_table_names())
    assert tables >= EXPECTED_TABLES
