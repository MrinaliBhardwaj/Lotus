"""Chunk stage (sync — Celery workers): parent-child chunking with full provenance.

Pinned at scaffold review: children target 300 tokens with 12% (36-token)
overlap, parents cap at 2000 tokens, measured with the embedding provider's
tokenizer. Chunks NEVER cross a section boundary (each chunk derives from one
section's block run), and table blocks stay atomic — never merged or split.

Provenance (hard invariant #1) is carried from the parse artifacts, never
reconstructed: char offsets index the canonical linearized stream (chunk
content is literally ``linearized.text[char_start:char_end]``), and bboxes
are the normalized per-page boxes of every member block.
"""

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.models import Chunk, ChunkType, Document, DocumentStatus, JobStage, JobState
from app.providers.embeddings.tokenizer import Tokenizer
from app.schemas.ir import LinearizedText
from app.services.ingestion import pipeline, status
from app.services.ingestion.parse import load_linearized, load_page_ir
from app.services.ingestion.structure import SECTION_MAP_ARTIFACT
from app.storage.base import ObjectStorage, artifact_key
from app.workers.session import worker_session

logger = logging.getLogger(__name__)

# a single block may exceed the target before it gets split on its own
_SPLIT_TOLERANCE = 1.2


@dataclass
class BlockUnit:
    """One block's contribution to a section, in linearized order."""

    id: str
    page: int
    bbox: list[float]
    type: str
    char_start: int
    char_end: int
    tokens: int


@dataclass
class ChunkDraft:
    chunk_type: ChunkType
    content: str
    page_start: int
    page_end: int
    bboxes: list[dict[str, Any]]
    char_start: int
    char_end: int
    section_path: str
    section_title: str
    block_ids: list[str]
    token_count: int
    children: list["ChunkDraft"]


def build_section_chunks(
    section: dict[str, Any],
    units: list[BlockUnit],
    text: str,
    tokenizer: Tokenizer,
    *,
    child_tokens: int,
    overlap_tokens: int,
    parent_tokens: int,
) -> list[ChunkDraft]:
    """Pure transform: one section's blocks → parent drafts with child drafts."""
    parents: list[ChunkDraft] = []
    for parent_units in _pack(units, text, tokenizer, parent_tokens, overlap=0):
        parent = _draft(ChunkType.PARENT, parent_units, text, section, tokenizer)
        parent.children = [
            _draft(ChunkType.CHILD, child_units, text, section, tokenizer)
            for child_units in _pack(
                parent_units, text, tokenizer, child_tokens, overlap=overlap_tokens
            )
        ]
        parents.append(parent)
    return parents


def run_chunk(document_id: uuid.UUID, *, settings: Settings, storage: ObjectStorage) -> None:
    from app.core.deps import get_embedding_provider

    tokenizer = get_embedding_provider(settings).tokenizer
    with worker_session() as session:
        document = session.get(Document, document_id)
        if document is None or document.status is not DocumentStatus.CHUNKING:
            logger.info("chunk(%s): stale or missing — no-op", document_id)
            return
        job = status.start_job_sync(session, document_id, JobStage.CHUNK)
        user_id = document.user_id

        linearized = load_linearized(user_id, document_id, storage)
        section_map = json.loads(
            asyncio.run(
                storage.get(artifact_key(user_id, document_id, SECTION_MAP_ARTIFACT))
            )
        )
        units_by_id = _units_by_block(
            user_id, document_id, document.page_count or 0, linearized, tokenizer, storage
        )

        drafts: list[ChunkDraft] = []
        for section in section_map["sections"]:
            units = [units_by_id[bid] for bid in section["block_ids"] if bid in units_by_id]
            if units:
                drafts.extend(
                    build_section_chunks(
                        section,
                        units,
                        linearized.text,
                        tokenizer,
                        child_tokens=settings.chunk_child_tokens,
                        overlap_tokens=settings.chunk_child_overlap_tokens,
                        parent_tokens=settings.chunk_parent_tokens,
                    )
                )

        # idempotent re-run: replace the document's chunks wholesale (one txn)
        session.query(Chunk).filter_by(document_id=document_id).delete()
        parent_count = child_count = 0
        for parent in drafts:
            parent_id = uuid.uuid4()
            session.add(_row(parent, parent_id, document_id, parent_chunk_id=None))
            parent_count += 1
            for child in parent.children:
                session.add(_row(child, uuid.uuid4(), document_id, parent_chunk_id=parent_id))
                child_count += 1
        session.commit()

        status.finish_job_sync(
            session,
            job,
            JobState.SUCCEEDED,
            checkpoint={"parents": parent_count, "children": child_count},
        )
        if status.transition_document_sync(
            session, document_id, DocumentStatus.CHUNKING, DocumentStatus.EMBEDDING
        ):
            session.commit()
            pipeline.enqueue_stage(JobStage.EMBED, document_id)


def _units_by_block(
    user_id: uuid.UUID,
    document_id: uuid.UUID,
    page_count: int,
    linearized: LinearizedText,
    tokenizer: Tokenizer,
    storage: ObjectStorage,
) -> dict[str, BlockUnit]:
    blocks_meta: dict[str, tuple[list[float], str]] = {}
    for page in range(1, page_count + 1):
        for block in load_page_ir(user_id, document_id, page, storage).blocks:
            blocks_meta[block.id] = (block.bbox, block.type)
    units: dict[str, BlockUnit] = {}
    for entry in linearized.blocks:
        bbox, block_type = blocks_meta[entry.id]
        text = linearized.text[entry.char_start : entry.char_end]
        units[entry.id] = BlockUnit(
            id=entry.id,
            page=entry.page,
            bbox=bbox,
            type=block_type,
            char_start=entry.char_start,
            char_end=entry.char_end,
            tokens=tokenizer.count(text),
        )
    return units


def _pack(
    units: list[BlockUnit],
    text: str,
    tokenizer: Tokenizer,
    cap: int,
    *,
    overlap: int,
) -> list[list[BlockUnit]]:
    """Group consecutive units into ≤ cap-token windows.

    Oversized text blocks are split by the tokenizer into overlapping
    sub-units (their char ranges stay exact); oversized tables stay atomic.
    Overlap between groups re-seeds each group with the previous group's tail.
    """
    expanded: list[BlockUnit] = []
    for unit in units:
        if unit.type != "table" and unit.tokens > cap * _SPLIT_TOLERANCE:
            block_text = text[unit.char_start : unit.char_end]
            for start, end in tokenizer.split(block_text, cap, overlap):
                sub_text = block_text[start:end]
                expanded.append(
                    BlockUnit(
                        id=unit.id,
                        page=unit.page,
                        bbox=unit.bbox,
                        type=unit.type,
                        char_start=unit.char_start + start,
                        char_end=unit.char_start + end,
                        tokens=tokenizer.count(sub_text),
                    )
                )
        else:
            expanded.append(unit)

    groups: list[list[BlockUnit]] = []
    current: list[BlockUnit] = []
    current_tokens = 0
    for unit in expanded:
        if current and current_tokens + unit.tokens > cap:
            groups.append(current)
            seed = _tail_seed(current[-1], text, tokenizer, overlap)
            if seed is not None and seed.char_end <= unit.char_start:
                current, current_tokens = [seed], seed.tokens
            else:
                current, current_tokens = [], 0
        current.append(unit)
        current_tokens += unit.tokens
    if current:
        groups.append(current)
    return groups


def _tail_seed(
    tail: BlockUnit, text: str, tokenizer: Tokenizer, overlap: int
) -> BlockUnit | None:
    """The ≤ overlap-token tail of the previous window, re-emitted at the start
    of the next one (the pinned ~12% overlap). Tables are never duplicated."""
    if overlap <= 0 or tail.type == "table":
        return None
    if tail.tokens <= overlap:
        return tail
    tail_text = text[tail.char_start : tail.char_end]
    start, end = tokenizer.split(tail_text, overlap, 0)[-1]
    return BlockUnit(
        id=tail.id,
        page=tail.page,
        bbox=tail.bbox,
        type=tail.type,
        char_start=tail.char_start + start,
        char_end=tail.char_start + end,
        tokens=tokenizer.count(tail_text[start:end]),
    )


def _draft(
    chunk_type: ChunkType,
    units: list[BlockUnit],
    text: str,
    section: dict[str, Any],
    tokenizer: Tokenizer,
) -> ChunkDraft:
    char_start, char_end = units[0].char_start, units[-1].char_end
    content = text[char_start:char_end]
    return ChunkDraft(
        chunk_type=chunk_type,
        content=content,
        page_start=min(u.page for u in units),
        page_end=max(u.page for u in units),
        bboxes=[{"page": u.page, "rect": u.bbox} for u in units],
        char_start=char_start,
        char_end=char_end,
        section_path=section["path"],
        section_title=section["title"],
        block_ids=list(dict.fromkeys(u.id for u in units)),
        token_count=tokenizer.count(content),
        children=[],
    )


def _row(
    draft: ChunkDraft,
    chunk_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    parent_chunk_id: uuid.UUID | None,
) -> Chunk:
    return Chunk(
        id=chunk_id,
        document_id=document_id,
        parent_chunk_id=parent_chunk_id,
        chunk_type=draft.chunk_type,
        content=draft.content,
        content_hash=hashlib.sha256(draft.content.encode()).hexdigest(),
        page_start=draft.page_start,
        page_end=draft.page_end,
        bboxes=draft.bboxes,
        char_start=draft.char_start,
        char_end=draft.char_end,
        section_path=draft.section_path,
        section_title=draft.section_title,
        block_ids=draft.block_ids,
        token_count=draft.token_count,
    )
