"""Task 3 gate tests: the validate/triage worker stage (sync path, §2.1 #7/#8)."""

import asyncio
import json
import uuid
from functools import partial
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models import DocumentStatus, JobStage, JobState
from app.services.ingestion.validate import MANIFEST_ARTIFACT, run_validate
from app.storage.base import artifact_key
from app.storage.local import LocalStorage
from tests.db_utils import load_state as _load_state
from tests.db_utils import seed_document
from tests.pdf_utils import make_pdf

load_state = partial(_load_state, stage=JobStage.VALIDATE)


@pytest.fixture
def storage(db_settings: Settings) -> LocalStorage:
    return LocalStorage(db_settings.local_storage_path)


def test_validate_happy_path(
    sync_session_factory: sessionmaker[Session],
    storage: LocalStorage,
    db_settings: Settings,
    captured_stages: list[tuple[JobStage, uuid.UUID]],
    tmp_path: Path,
) -> None:
    document_id = seed_document(sync_session_factory, storage, make_pdf(pages=4))
    run_validate(document_id, settings=db_settings, storage=storage)

    document, job = load_state(sync_session_factory, document_id)
    assert document.status is DocumentStatus.PARSING
    assert document.page_count == 4
    assert job is not None and job.state is JobState.SUCCEEDED
    assert job.checkpoint["counts"] == {"text": 4, "scanned": 0, "mixed": 0}
    assert captured_stages == [(JobStage.PARSE, document_id)]

    manifest_raw = asyncio.run(
        storage.get(artifact_key(document.user_id, document_id, MANIFEST_ARTIFACT))
    )
    manifest = json.loads(manifest_raw)
    assert [p["page"] for p in manifest["pages"]] == [1, 2, 3, 4]
    assert all(p["mode"] == "text" for p in manifest["pages"])


def test_validate_rejects_encrypted_pdf(
    sync_session_factory: sessionmaker[Session],
    storage: LocalStorage,
    db_settings: Settings,
    captured_stages: list[tuple[JobStage, uuid.UUID]],
) -> None:
    document_id = seed_document(sync_session_factory, storage, make_pdf(pages=2, encrypted=True))
    run_validate(document_id, settings=db_settings, storage=storage)

    document, job = load_state(sync_session_factory, document_id)
    assert document.status is DocumentStatus.FAILED
    assert job is not None and job.state is JobState.DEAD_LETTER
    assert job.error is not None and "encrypted" in job.error
    assert captured_stages == []


def test_validate_rejects_scanned_pages_needing_ocr(
    sync_session_factory: sessionmaker[Session],
    storage: LocalStorage,
    db_settings: Settings,
    captured_stages: list[tuple[JobStage, uuid.UUID]],
) -> None:
    document_id = seed_document(
        sync_session_factory, storage, make_pdf(pages=3, scanned_pages={2, 3})
    )
    run_validate(document_id, settings=db_settings, storage=storage)

    document, job = load_state(sync_session_factory, document_id)
    assert document.status is DocumentStatus.FAILED
    assert job is not None and job.error is not None and "OCR" in job.error
    assert captured_stages == []

    # triage evidence survives: the manifest was stored before the rejection
    manifest = json.loads(
        asyncio.run(storage.get(artifact_key(document.user_id, document_id, MANIFEST_ARTIFACT)))
    )
    assert manifest["counts"]["scanned"] == 2


def test_validate_enforces_page_ceiling(
    sync_session_factory: sessionmaker[Session],
    storage: LocalStorage,
    db_settings: Settings,
) -> None:
    small_ceiling = db_settings.model_copy(update={"max_pdf_pages": 2})
    document_id = seed_document(sync_session_factory, storage, make_pdf(pages=3))
    run_validate(document_id, settings=small_ceiling, storage=storage)

    document, job = load_state(sync_session_factory, document_id)
    assert document.status is DocumentStatus.FAILED
    assert job is not None and job.error is not None and "limit is 2" in job.error


def test_validate_rejects_garbage_bytes(
    sync_session_factory: sessionmaker[Session],
    storage: LocalStorage,
    db_settings: Settings,
) -> None:
    document_id = seed_document(sync_session_factory, storage, b"%PDF-not really a pdf")
    run_validate(document_id, settings=db_settings, storage=storage)

    document, job = load_state(sync_session_factory, document_id)
    assert document.status is DocumentStatus.FAILED
    assert job is not None and job.error is not None and "opened" in job.error


def test_validate_is_noop_on_stale_status(
    sync_session_factory: sessionmaker[Session],
    storage: LocalStorage,
    db_settings: Settings,
    captured_stages: list[tuple[JobStage, uuid.UUID]],
) -> None:
    document_id = seed_document(
        sync_session_factory, storage, make_pdf(pages=1), status=DocumentStatus.READY
    )
    run_validate(document_id, settings=db_settings, storage=storage)

    document, job = load_state(sync_session_factory, document_id)
    assert document.status is DocumentStatus.READY  # guarded transition: no-op
    assert job is None
    assert captured_stages == []
