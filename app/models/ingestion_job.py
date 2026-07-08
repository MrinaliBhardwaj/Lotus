import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class JobStage(enum.StrEnum):
    VALIDATE = "validate"
    PARSE = "parse"
    OCR = "ocr"  # Phase 1 rejects docs needing OCR; the stage exists so it's not a retrofit
    STRUCTURE = "structure"
    CHUNK = "chunk"
    EMBED = "embed"
    INDEX = "index"


class JobState(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # transient failure, retryable
    DEAD_LETTER = "dead_letter"  # permanent failure, human attention


class IngestionJob(Base, TimestampMixin):
    """One row per (document, stage) — a retried stage updates its row, never
    duplicates it (CLAUDE.md §2.1 #8). ``checkpoint`` carries page-level
    progress so a crashed stage resumes instead of restarting."""

    __tablename__ = "ingestion_jobs"
    __table_args__ = (UniqueConstraint("document_id", "stage"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[JobStage] = mapped_column(
        Enum(JobStage, name="job_stage", values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, name="job_state", values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        server_default=JobState.QUEUED.value,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
