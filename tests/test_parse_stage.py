"""Task 4 gate tests: the page-parallel parse stage writes IR + linearized text."""

import uuid
from functools import partial

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models import DocumentStatus, JobStage, JobState
from app.parsers.pymupdf_parser import PyMuPDFParser
from app.services.ingestion import pipeline
from app.services.ingestion.parse import (
    load_linearized,
    load_page_ir,
    run_parse,
    run_parse_batch,
)
from app.storage.local import LocalStorage
from tests.db_utils import load_state as _load_state
from tests.db_utils import seed_document
from tests.pdf_utils import make_pdf

load_state = partial(_load_state, stage=JobStage.PARSE)


@pytest.fixture
def storage(db_settings: Settings) -> LocalStorage:
    return LocalStorage(db_settings.local_storage_path)


@pytest.fixture
def inline_batches(
    monkeypatch: pytest.MonkeyPatch, db_settings: Settings, storage: LocalStorage
) -> list[tuple[int, int]]:
    """Run fan-out batches synchronously in-process (no broker)."""
    calls: list[tuple[int, int]] = []
    parser = PyMuPDFParser()

    def _inline(document_id: uuid.UUID, page_start: int, page_end: int) -> None:
        calls.append((page_start, page_end))
        run_parse_batch(
            document_id,
            page_start,
            page_end,
            settings=db_settings,
            storage=storage,
            parser=parser,
        )

    monkeypatch.setattr(pipeline, "enqueue_parse_batch", _inline)
    return calls


def test_parse_stage_end_to_end_100_plus_pages(
    sync_session_factory: sessionmaker[Session],
    storage: LocalStorage,
    db_settings: Settings,
    captured_stages: list[tuple[JobStage, uuid.UUID]],
    inline_batches: list[tuple[int, int]],
) -> None:
    document_id = seed_document(
        sync_session_factory,
        storage,
        make_pdf(pages=120),
        status=DocumentStatus.PARSING,
        page_count=120,
    )
    run_parse(document_id, settings=db_settings)

    # 120 pages / 50 per batch → 3 parallel batches
    assert inline_batches == [(1, 50), (51, 100), (101, 120)]

    document, job = load_state(sync_session_factory, document_id)
    assert document.status is DocumentStatus.STRUCTURING
    assert job is not None and job.state is JobState.SUCCEEDED
    assert len(job.checkpoint["batches_done"]) == 3
    assert captured_stages == [(JobStage.STRUCTURE, document_id)]

    # spot-check IR pages (the gate): correct page numbers, normalized bboxes
    for page_number in (1, 60, 120):
        page_ir = load_page_ir(document.user_id, document_id, page_number, storage)
        assert page_ir.page == page_number
        assert page_ir.blocks
        assert all(0.0 <= v <= 1.0 for b in page_ir.blocks for v in b.bbox)

    # linearized stream: every block's char span slices back to its exact text
    linearized = load_linearized(document.user_id, document_id, storage)
    assert linearized.text
    by_page: dict[int, dict[str, str]] = {}
    for entry in linearized.blocks:
        page_ir = by_page.setdefault(
            entry.page,
            {
                b.id: b.text
                for b in load_page_ir(document.user_id, document_id, entry.page, storage).blocks
            },
        )
        assert linearized.text[entry.char_start : entry.char_end] == page_ir[entry.id]
    # offsets are strictly increasing and non-overlapping
    spans = [(e.char_start, e.char_end) for e in linearized.blocks]
    assert spans == sorted(spans)
    assert all(a_end <= b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:], strict=False))


def test_parse_batch_rerun_is_idempotent(
    sync_session_factory: sessionmaker[Session],
    storage: LocalStorage,
    db_settings: Settings,
    captured_stages: list[tuple[JobStage, uuid.UUID]],
    inline_batches: list[tuple[int, int]],
) -> None:
    document_id = seed_document(
        sync_session_factory,
        storage,
        make_pdf(pages=3),
        status=DocumentStatus.PARSING,
        page_count=3,
    )
    run_parse(document_id, settings=db_settings)
    # a redelivered batch task (acks_late crash-requeue) is a no-op afterwards
    run_parse_batch(
        document_id, 1, 3, settings=db_settings, storage=storage, parser=PyMuPDFParser()
    )
    document, job = load_state(sync_session_factory, document_id)
    assert document.status is DocumentStatus.STRUCTURING
    assert job is not None and len(job.checkpoint["batches_done"]) == 1
    assert captured_stages == [(JobStage.STRUCTURE, document_id)]


def test_parse_is_noop_on_stale_status(
    sync_session_factory: sessionmaker[Session],
    storage: LocalStorage,
    db_settings: Settings,
    inline_batches: list[tuple[int, int]],
) -> None:
    document_id = seed_document(
        sync_session_factory,
        storage,
        make_pdf(pages=2),
        status=DocumentStatus.READY,
        page_count=2,
    )
    run_parse(document_id, settings=db_settings)
    assert inline_batches == []
    document, job = load_state(sync_session_factory, document_id)
    assert document.status is DocumentStatus.READY
    assert job is None
