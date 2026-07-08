import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class DocumentStatus(enum.StrEnum):
    """DESIGN.md §8. Transitions are guarded (CLAUDE.md §2.1 #8):
    UPDATE ... WHERE status = <expected> — a stale transition is a no-op."""

    UPLOADED = "UPLOADED"
    VALIDATING = "VALIDATING"
    PARSING = "PARSING"
    OCR = "OCR"
    STRUCTURING = "STRUCTURING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    READY = "READY"
    FAILED = "FAILED"
    DEGRADED = "DEGRADED"


class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (
        # SHA-256 dedupe is PER USER, never corpus-wide (CLAUDE.md §2.1 #3)
        UniqueConstraint("user_id", "doc_hash"),
        Index("ix_documents_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
        server_default=DocumentStatus.UPLOADED.value,
    )
    doc_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # set at finalize
    mime: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # soft delete — S3 objects and DB rows are reaped together by a cleanup job
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
