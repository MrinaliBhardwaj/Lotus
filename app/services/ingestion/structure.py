"""Structure stage (sync — Celery workers): heading detection → section tree.

Two signals, per CLAUDE.md Task 5: font-vs-body-baseline (headings are set
larger/bolder than the dominant body style) and a numbering grammar
(``1.``, ``1.2.3``, ``Article IV``, ``Section 12``). Detected sections are
persisted to the ``sections`` table and to a section-map artifact assigning
every block to exactly one section — the chunker consumes the artifact, so
chunking stays a pure transform over parse-stage outputs (invariant 3).
"""

import asyncio
import json
import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field

from app.core.config import Settings
from app.models import Document, DocumentStatus, JobStage, JobState, Section
from app.schemas.ir import BlockIR, PageIR
from app.services.ingestion import pipeline, status
from app.services.ingestion.parse import load_page_ir
from app.storage.base import ObjectStorage, artifact_key
from app.workers.session import worker_session

logger = logging.getLogger(__name__)

SECTION_MAP_ARTIFACT = "ir/section_map.json"

_NUMBERED = re.compile(r"^(?P<label>\d+(?:\.\d+)*)[.)]?\s+\S")
_ARTICLE = re.compile(
    r"^(?P<kind>article|section)\s+(?P<num>[ivxlcdm]+|\d+)\b", re.IGNORECASE
)
_HEADING_MAX_CHARS = 120
_HEADING_MAX_LINES = 2
_FONT_RATIO = 1.15  # heading font must exceed the body baseline by this factor


@dataclass
class DetectedSection:
    index: int
    parent_index: int | None
    level: int
    label: str  # numbering label ("12.3") or truncated title
    title: str
    page_start: int
    path: str = ""
    block_ids: list[str] = field(default_factory=list)


def detect_sections(pages: list[PageIR]) -> list[DetectedSection]:
    baseline = _body_baseline(pages)
    size_levels = _font_size_levels(pages, baseline)

    root = DetectedSection(
        index=0, parent_index=None, level=0, label="", title="Preamble", page_start=1
    )
    root.path = "Preamble"
    sections = [root]
    stack = [root]

    for page in pages:
        for block in page.blocks:
            heading = _heading_of(block, baseline, size_levels)
            if heading is None:
                stack[-1].block_ids.append(block.id)
                continue
            level, label = heading
            while stack[-1].level >= level:
                stack.pop()
            parent = stack[-1]
            title = " ".join(block.text.split())
            section = DetectedSection(
                index=len(sections),
                parent_index=parent.index,
                level=level,
                label=label or title[:40],
                title=title,
                page_start=page.page,
            )
            section.path = (
                f"{parent.path} > {section.label}" if parent.index != 0 else section.label
            )
            section.block_ids.append(block.id)
            sections.append(section)
            stack.append(section)

    return [s for s in sections if s.block_ids]


def run_structure(document_id: uuid.UUID, *, settings: Settings, storage: ObjectStorage) -> None:
    with worker_session() as session:
        document = session.get(Document, document_id)
        if document is None or document.status is not DocumentStatus.STRUCTURING:
            logger.info("structure(%s): stale or missing — no-op", document_id)
            return
        job = status.start_job_sync(session, document_id, JobStage.STRUCTURE)
        user_id, page_count = document.user_id, document.page_count or 0

        pages = [load_page_ir(user_id, document_id, p, storage) for p in range(1, page_count + 1)]
        detected = detect_sections(pages)

        # idempotent re-run: replace this document's tree wholesale
        session.query(Section).filter_by(document_id=document_id).delete()
        ids: dict[int, uuid.UUID] = {}
        for section in detected:
            section_id = uuid.uuid4()
            ids[section.index] = section_id
            parent_id = (
                ids.get(section.parent_index) if section.parent_index is not None else None
            )
            session.add(
                Section(
                    id=section_id,
                    document_id=document_id,
                    parent_id=parent_id,
                    level=section.level,
                    title=section.title,
                    section_path=section.path,
                    page_start=section.page_start,
                )
            )
        session.commit()

        section_map = {
            "sections": [
                {
                    "id": str(ids[s.index]),
                    "path": s.path,
                    "title": s.title,
                    "level": s.level,
                    "page_start": s.page_start,
                    "block_ids": s.block_ids,
                }
                for s in detected
            ]
        }
        asyncio.run(
            storage.put(
                artifact_key(user_id, document_id, SECTION_MAP_ARTIFACT),
                json.dumps(section_map).encode(),
                content_type="application/json",
            )
        )

        status.finish_job_sync(
            session, job, JobState.SUCCEEDED, checkpoint={"sections": len(detected)}
        )
        if status.transition_document_sync(
            session, document_id, DocumentStatus.STRUCTURING, DocumentStatus.CHUNKING
        ):
            session.commit()
            pipeline.enqueue_stage(JobStage.CHUNK, document_id)


def _body_baseline(pages: list[PageIR]) -> float:
    """The dominant body font size, weighted by how much text it carries."""
    weights: Counter[float] = Counter()
    for page in pages:
        for block in page.blocks:
            if block.type == "text" and block.font_size:
                weights[round(block.font_size, 1)] += len(block.text)
    return weights.most_common(1)[0][0] if weights else 10.0


def _font_size_levels(pages: list[PageIR], baseline: float) -> dict[float, int]:
    """Rank distinct heading-sized fonts: biggest → level 1, next → 2, …"""
    sizes = sorted(
        {
            round(block.font_size, 1)
            for page in pages
            for block in page.blocks
            if block.font_size and block.font_size > baseline * _FONT_RATIO
        },
        reverse=True,
    )
    return {size: min(rank + 1, 6) for rank, size in enumerate(sizes)}


def _heading_of(
    block: BlockIR, baseline: float, size_levels: dict[float, int]
) -> tuple[int, str] | None:
    """(level, numbering label) if the block is a heading, else None."""
    if block.type != "text" or not block.text:
        return None
    if len(block.text) > _HEADING_MAX_CHARS or block.text.count("\n") >= _HEADING_MAX_LINES:
        return None
    first_line = block.text.split("\n")[0].strip()

    numbered = _NUMBERED.match(first_line)
    article = _ARTICLE.match(first_line)
    looks_emphasized = bool(
        block.font_size
        and (
            block.font_size > baseline * _FONT_RATIO
            or (block.is_bold and block.font_size >= baseline)
        )
    )

    if numbered and (looks_emphasized or "\n" not in block.text):
        label = numbered.group("label")
        return label.count(".") + 1, label
    if article and (looks_emphasized or "\n" not in block.text):
        kind = article.group("kind").capitalize()
        level = 1 if kind == "Article" else 2
        return level, f"{kind} {article.group('num').upper()}"
    if looks_emphasized and block.font_size:
        size_level = size_levels.get(round(block.font_size, 1))
        if size_level is not None:
            return size_level, ""
    return None
