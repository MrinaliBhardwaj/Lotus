"""The chunks table — full provenance from day one (hard invariant #1).

Partitioning (CLAUDE.md §2.1 #4): HASH (document_id), 16 partitions. Postgres
requires the partition key in the primary key, so the PK is (id, document_id)
and the parent-child self-FK is the composite (parent_chunk_id, document_id) →
(id, document_id) — parent and child always share a document.

Lexical search (§2.1 #10): two STORED generated tsvector columns — 'english'
(stemmed) and 'simple' (exact; defined terms and clause numbers must not be
stemmed away). GIN indexes on both; HNSW on the embedding. All three are
partitioned indexes created in the migration.
"""

import enum
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin

EMBEDDING_DIMENSIONS = 1536  # pinned with text-embedding-3-small (§2.1 #5)
CHUNK_PARTITION_COUNT = 16  # pinned; effectively immutable after creation (§2.1 #4)


class ChunkType(enum.StrEnum):
    PARENT = "parent"  # logical section — context handed to the model
    CHILD = "child"  # ~300-token sub-chunk — the embedded/retrieved unit


class Chunk(Base, TimestampMixin):
    __tablename__ = "chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["parent_chunk_id", "document_id"],
            ["chunks.id", "chunks.document_id"],
            ondelete="CASCADE",
        ),
        # invariant 1: a chunk's footprint is a non-empty LIST of page rects
        CheckConstraint(
            "jsonb_typeof(bboxes) = 'array' AND jsonb_array_length(bboxes) > 0",
            name="bboxes_nonempty_array",
        ),
        CheckConstraint("char_end >= char_start", name="char_range_valid"),
        CheckConstraint("page_end >= page_start", name="page_range_valid"),
        Index("ix_chunks_document_id_chunk_type", "document_id", "chunk_type"),
        Index("ix_chunks_parent", "parent_chunk_id", "document_id"),
        {"postgresql_partition_by": "HASH (document_id)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,  # partition key must be part of the PK
    )
    parent_chunk_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    chunk_type: Mapped[ChunkType] = mapped_column(
        Enum(ChunkType, name="chunk_type", values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)  # embed-stage dedupe (§4.8)

    # --- embedding (populated at the embed stage; model/version enable
    # selective re-embedding without re-parsing) -----------------------------
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)
    embed_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    embed_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- provenance (invariant 1 — captured at parse time, never reconstructed,
    # complete in Phase 1 even though highlights render in Phase 2) -----------
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    bboxes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    # ^ LIST of {"page": int, "rect": [x0, y0, x1, y1]} — normalized 0-1 (invariant 2)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)  # global offsets into the
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)  # canonical linearized text
    section_path: Mapped[str] = mapped_column(Text, nullable=False)
    section_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    block_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)  # BlockIR lineage
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    min_ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- lexical search (generated, never written by the app) ----------------
    tsv_english: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True), nullable=False
    )
    tsv_simple: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True), nullable=False
    )
