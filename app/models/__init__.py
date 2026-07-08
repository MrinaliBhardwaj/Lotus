"""SQLAlchemy ORM models.

This module imports every model so Alembic autogenerate and metadata-driven
tooling see the full schema. Keep it the single registration point.
"""

from app.models.chat import Chat
from app.models.chunk import CHUNK_PARTITION_COUNT, EMBEDDING_DIMENSIONS, Chunk, ChunkType
from app.models.document import Document, DocumentStatus
from app.models.ingestion_job import IngestionJob, JobStage, JobState
from app.models.message import Message, MessageRole
from app.models.section import Section
from app.models.user import User

__all__ = [
    "CHUNK_PARTITION_COUNT",
    "EMBEDDING_DIMENSIONS",
    "Chat",
    "Chunk",
    "ChunkType",
    "Document",
    "DocumentStatus",
    "IngestionJob",
    "JobStage",
    "JobState",
    "Message",
    "MessageRole",
    "Section",
    "User",
]
