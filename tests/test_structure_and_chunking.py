"""Task 5 gate tests: section detection + parent-child chunking with provenance.

Gate: chunks exist with FULL provenance; no chunk crosses a section boundary;
every chunk has a non-empty bboxes list.
"""

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models import Chunk, ChunkType, DocumentStatus, JobStage, Section
from app.parsers.pymupdf_parser import PyMuPDFParser
from app.services.ingestion.parse import load_linearized
from app.services.ingestion.structure import detect_sections
from app.services.ingestion.validate import run_validate
from app.storage.local import LocalStorage
from tests.db_utils import load_state, seed_document
from tests.pdf_utils import make_contract_pdf

# --- pure heading detection ---------------------------------------------------


def test_detect_sections_numbering_and_hierarchy() -> None:
    parser = PyMuPDFParser()
    data = make_contract_pdf(section_count=2)
    import fitz

    with fitz.open(stream=data, filetype="pdf") as pdf:
        page_count = pdf.page_count
    pages = parser.parse_pages(data, range(1, page_count + 1))

    sections = detect_sections(pages)
    paths = [s.path for s in sections]
    assert "1" in paths and "2" in paths
    assert "1 > 1.1" in paths and "2 > 2.1" in paths

    by_path = {s.path: s for s in sections}
    assert by_path["1"].level == 1
    assert by_path["1 > 1.1"].level == 2
    assert by_path["1 > 1.1"].parent_index == by_path["1"].index
    assert by_path["1"].title.startswith("1. Section 1 Heading")
    # every block belongs to exactly one section
    all_blocks = [bid for s in sections for bid in s.block_ids]
    assert len(all_blocks) == len(set(all_blocks))
    assert len(all_blocks) == sum(len(p.blocks) for p in pages)


# --- the full validate → parse → structure → chunk chain ------------------------


def test_structure_and_chunk_stages_end_to_end(
    sync_session_factory: sessionmaker[Session],
    db_settings: Settings,
    inline_pipeline: list[JobStage],
) -> None:
    storage = LocalStorage(db_settings.local_storage_path)
    document_id = seed_document(sync_session_factory, storage, make_contract_pdf())
    run_validate(document_id, settings=db_settings, storage=storage)

    assert inline_pipeline[:3] == [JobStage.PARSE, JobStage.STRUCTURE, JobStage.CHUNK]
    document, chunk_job = load_state(sync_session_factory, document_id, JobStage.CHUNK)
    assert document.status is DocumentStatus.READY  # chunk handed off; chain completed
    assert chunk_job is not None
    assert chunk_job.checkpoint["parents"] >= 6  # 3 sections + 3 subsections
    assert chunk_job.checkpoint["children"] > chunk_job.checkpoint["parents"]

    linearized = load_linearized(document.user_id, document_id, storage)

    with sync_session_factory() as session:
        sections = session.scalars(
            select(Section).where(Section.document_id == document_id)
        ).all()
        chunks = session.scalars(
            select(Chunk).where(Chunk.document_id == document_id)
        ).all()

    section_paths = {s.section_path for s in sections}
    assert {"1", "1 > 1.1", "2", "2 > 2.1", "3", "3 > 3.1"} <= section_paths

    parents = [c for c in chunks if c.chunk_type is ChunkType.PARENT]
    children = [c for c in chunks if c.chunk_type is ChunkType.CHILD]
    assert parents and children
    parent_ids = {p.id for p in parents}

    for chunk in chunks:
        # full provenance, invariant 1 — no field deferred
        assert chunk.bboxes, "empty bboxes"
        assert all(
            0.0 <= v <= 1.0 for b in chunk.bboxes for v in b["rect"]
        ), "bbox not normalized"
        assert all(chunk.page_start <= b["page"] <= chunk.page_end for b in chunk.bboxes)
        assert chunk.section_path in section_paths
        assert chunk.block_ids
        assert chunk.token_count > 0
        # char offsets index the canonical linearized stream EXACTLY
        assert chunk.content == linearized.text[chunk.char_start : chunk.char_end]
        assert chunk.content_hash == hashlib.sha256(chunk.content.encode()).hexdigest()

    for child in children:
        assert child.parent_chunk_id in parent_ids
        # 300-token target with 1.2x split tolerance
        assert child.token_count <= db_settings.chunk_child_tokens * 1.2 + 1

    # children never cross their parent's section (no cross-section splits)
    parents_by_id = {p.id: p for p in parents}
    for child in children:
        parent = parents_by_id[child.parent_chunk_id]
        assert child.section_path == parent.section_path
        assert parent.char_start <= child.char_start
        assert child.char_end <= parent.char_end

    # consecutive children of one parent overlap (~12% pinned)
    for parent in parents:
        siblings = sorted(
            (c for c in children if c.parent_chunk_id == parent.id),
            key=lambda c: c.char_start,
        )
        for left, right in zip(siblings, siblings[1:], strict=False):
            assert right.char_start < left.char_end, "no overlap between siblings"


def test_chunk_rerun_is_idempotent(
    sync_session_factory: sessionmaker[Session],
    db_settings: Settings,
    inline_pipeline: list[JobStage],
) -> None:
    from app.services.ingestion.chunking import run_chunk
    from app.services.ingestion.status import transition_document_sync

    storage = LocalStorage(db_settings.local_storage_path)
    document_id = seed_document(sync_session_factory, storage, make_contract_pdf(2, 3))
    run_validate(document_id, settings=db_settings, storage=storage)

    def _count() -> int:
        with sync_session_factory() as session:
            return len(
                session.scalars(select(Chunk.id).where(Chunk.document_id == document_id)).all()
            )

    first = _count()
    assert first > 0
    # simulate a crash-requeue: force the doc back to CHUNKING and re-run
    with sync_session_factory() as session:
        assert transition_document_sync(
            session, document_id, DocumentStatus.READY, DocumentStatus.CHUNKING
        )
        session.commit()
    run_chunk(document_id, settings=db_settings, storage=storage)
    assert _count() == first  # replaced wholesale, not duplicated
