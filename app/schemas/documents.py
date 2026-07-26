"""Document DTOs — the API contract; ORM objects never cross the router boundary (§3)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import DocumentStatus, JobStage, JobState


class DocumentCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=512)


class DocumentCreateResponse(BaseModel):
    id: uuid.UUID
    title: str
    upload_url: str
    upload_method: str  # "POST" (S3 policy form) or "PUT" (local dev)
    upload_fields: dict[str, str]  # form fields for a POST upload; empty for PUT
    upload_expires_in: int
    max_upload_bytes: int


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    status: DocumentStatus
    page_count: int | None
    size_bytes: int | None
    created_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    stage: JobStage
    state: JobState
    error: str | None


class DocumentCompleteResponse(BaseModel):
    document_id: uuid.UUID
    job_id: uuid.UUID
    status: DocumentStatus
