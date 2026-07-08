"""phase-1 core tables (DESIGN.md §8)

users, documents, sections, chunks (hash-partitioned, full provenance),
ingestion_jobs, chats, messages.

chunks partitioning (CLAUDE.md §2.1 #4): HASH (document_id), 16 partitions —
the count is effectively immutable after creation. The PK is (id, document_id)
because Postgres requires the partition key in the primary key; the
parent-child self-FK is composite for the same reason. HNSW + GIN indexes are
partitioned indexes: created once on the parent, cascaded to every partition.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from app.models.chunk import CHUNK_PARTITION_COUNT, EMBEDDING_DIMENSIONS

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

document_status = postgresql.ENUM(
    "UPLOADED",
    "VALIDATING",
    "PARSING",
    "OCR",
    "STRUCTURING",
    "CHUNKING",
    "EMBEDDING",
    "INDEXING",
    "READY",
    "FAILED",
    "DEGRADED",
    name="document_status",
    create_type=False,
)
chunk_type = postgresql.ENUM("parent", "child", name="chunk_type", create_type=False)
job_stage = postgresql.ENUM(
    "validate",
    "parse",
    "ocr",
    "structure",
    "chunk",
    "embed",
    "index",
    name="job_stage",
    create_type=False,
)
job_state = postgresql.ENUM(
    "queued", "running", "succeeded", "failed", "dead_letter", name="job_state", create_type=False
)
message_role = postgresql.ENUM("user", "assistant", name="message_role", create_type=False)

_ENUMS = (document_status, chunk_type, job_stage, job_state, message_role)


def _timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for enum in _ENUMS:
        enum.create(bind, checkfirst=True)

    # --- users ----------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("hashed_pw", sa.String(255), nullable=False),
        sa.Column("plan", sa.String(32), nullable=False, server_default="free"),
        *_timestamps(),
    )

    # --- documents --------------------------------------------------------------
    op.create_table(
        "documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("s3_key", sa.String(1024), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("status", document_status, nullable=False, server_default="UPLOADED"),
        sa.Column("doc_hash", sa.String(64), nullable=True),
        sa.Column("mime", sa.String(255), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        # SHA-256 dedupe is PER USER, never corpus-wide (§2.1 #3)
        sa.UniqueConstraint("user_id", "doc_hash", name="uq_documents_user_id"),
    )
    op.create_index("ix_documents_user_id_created_at", "documents", ["user_id", "created_at"])

    # --- sections ------------------------------------------------------------------
    op.create_table(
        "sections",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sections.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("section_path", sa.Text(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_sections_document_id", "sections", ["document_id"])

    # --- chunks (hash-partitioned) ---------------------------------------------------
    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("parent_chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chunk_type", chunk_type, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("embed_model", sa.Text(), nullable=True),
        sa.Column("embed_version", sa.Text(), nullable=True),
        # provenance — captured at parse time, never reconstructed (invariant 1)
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("bboxes", postgresql.JSONB(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("section_path", sa.Text(), nullable=False),
        sa.Column("section_title", sa.Text(), nullable=True),
        sa.Column("block_ids", postgresql.JSONB(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("min_ocr_confidence", sa.Float(), nullable=True),
        # lexical search — generated, never written by the app (§2.1 #10)
        sa.Column(
            "tsv_english",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "tsv_simple",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', content)", persisted=True),
            nullable=False,
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", "document_id", name="pk_chunks"),
        sa.ForeignKeyConstraint(
            ["parent_chunk_id", "document_id"],
            ["chunks.id", "chunks.document_id"],
            name="fk_chunks_parent_chunk_id_chunks",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(bboxes) = 'array' AND jsonb_array_length(bboxes) > 0",
            name="bboxes_nonempty_array",
        ),
        sa.CheckConstraint("char_end >= char_start", name="char_range_valid"),
        sa.CheckConstraint("page_end >= page_start", name="page_range_valid"),
        postgresql_partition_by="HASH (document_id)",
    )
    for i in range(CHUNK_PARTITION_COUNT):
        op.execute(
            f"CREATE TABLE chunks_p{i:02d} PARTITION OF chunks "
            f"FOR VALUES WITH (MODULUS {CHUNK_PARTITION_COUNT}, REMAINDER {i})"
        )
    # partitioned indexes — created on the parent, cascaded to every partition
    op.create_index("ix_chunks_document_id_chunk_type", "chunks", ["document_id", "chunk_type"])
    op.create_index("ix_chunks_parent", "chunks", ["parent_chunk_id", "document_id"])
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_chunks_tsv_english", "chunks", ["tsv_english"], postgresql_using="gin")
    op.create_index("ix_chunks_tsv_simple", "chunks", ["tsv_simple"], postgresql_using="gin")

    # --- ingestion_jobs ------------------------------------------------------------
    op.create_table(
        "ingestion_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", job_stage, nullable=False),
        sa.Column("state", job_state, nullable=False, server_default="queued"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("checkpoint", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        # a retried stage updates its row, never duplicates it (§2.1 #8)
        sa.UniqueConstraint("document_id", "stage", name="uq_ingestion_jobs_document_id"),
    )

    # --- chats / messages -------------------------------------------------------------
    op.create_table(
        "chats",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(512), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_chats_user_id", "chats", ["user_id"])

    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "chat_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", message_role, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", postgresql.JSONB(), nullable=True),
        sa.Column("token_usage", postgresql.JSONB(), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_messages_chat_id_created_at", "messages", ["chat_id", "created_at"])


def downgrade() -> None:
    # dropping the partitioned parent drops its partitions
    op.drop_table("messages")
    op.drop_table("chats")
    op.drop_table("ingestion_jobs")
    op.drop_table("chunks")
    op.drop_table("sections")
    op.drop_table("documents")
    op.drop_table("users")
    bind = op.get_bind()
    for enum in _ENUMS:
        enum.drop(bind, checkfirst=True)
