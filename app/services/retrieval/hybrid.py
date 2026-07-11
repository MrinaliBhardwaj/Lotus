"""Hybrid retrieval (Task 7): dense ANN ∥ lexical → RRF → children → parents.

Both arms run against ONE document partition (invariant 8 — single-doc
retrieval never scans the corpus) after a tenant-ownership check (invariant
7). RRF consumes ranks only (§2.1 #10), so the lexical arm's rank source
(ts_rank over the english + simple configs) never needs score calibration
against cosine distances. Children are the retrieval unit; matches expand to
their parent chunks, which carry the surrounding section context to the LLM.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import deps
from app.core.config import Settings
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.models import Chunk, ChunkType, Document, DocumentStatus


@dataclass(frozen=True)
class Source:
    """One [S#] source handed to the LLM: an ephemeral per-request id bound to
    a parent chunk. Pages/bboxes resolve server-side from the chunk row —
    the model only ever echoes the sid (invariant 5)."""

    sid: int
    chunk: Chunk


def rrf_fuse(
    rankings: Sequence[Sequence[uuid.UUID]], k: int = 60
) -> list[uuid.UUID]:
    """Reciprocal Rank Fusion: score(d) = Σ 1/(k + rank_i(d)). Ranks only —
    never raw scores — so heterogeneous arms fuse without calibration."""
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: (-scores[item], str(item)))


async def retrieve_sources(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
    query: str,
) -> list[Source]:
    document = await session.get(Document, document_id)
    if document is None or document.user_id != user_id or document.deleted_at is not None:
        raise NotFoundError("document not found")
    if document.status is not DocumentStatus.READY:
        raise ValidationFailedError(f"document is not ready (status: {document.status.value})")

    def children_of_document() -> Select[tuple[uuid.UUID]]:
        return select(Chunk.id).where(
            Chunk.document_id == document_id, Chunk.chunk_type == ChunkType.CHILD
        )

    # dense arm
    provider = deps.get_embedding_provider(settings)
    [query_vector] = await provider.embed([query])
    dense_ids = list(
        await session.scalars(
            children_of_document()
            .where(Chunk.embedding.is_not(None))
            .order_by(Chunk.embedding.cosine_distance(query_vector))
            .limit(settings.retrieval_dense_k)
        )
    )

    # lexical arm — both configs, each queried with its own config (§2.1 #10)
    english_query = func.plainto_tsquery("english", query)
    simple_query = func.plainto_tsquery("simple", query)
    lexical_rank = func.greatest(
        func.ts_rank(Chunk.tsv_english, english_query),
        func.ts_rank(Chunk.tsv_simple, simple_query),
    )
    lexical_ids = list(
        await session.scalars(
            children_of_document()
            .where(
                or_(
                    Chunk.tsv_english.op("@@")(english_query),
                    Chunk.tsv_simple.op("@@")(simple_query),
                )
            )
            .order_by(lexical_rank.desc())
            .limit(settings.retrieval_lexical_k)
        )
    )

    fused = rrf_fuse([dense_ids, lexical_ids], k=settings.retrieval_rrf_k)
    top_children = fused[: settings.retrieval_children_k]
    if not top_children:
        return []

    children = {
        chunk.id: chunk
        for chunk in await session.scalars(
            select(Chunk).where(Chunk.document_id == document_id, Chunk.id.in_(top_children))
        )
    }

    # expand to parents, preserving fused order, deduped
    parent_ids: list[uuid.UUID] = []
    for child_id in top_children:
        parent_id = children[child_id].parent_chunk_id
        if parent_id is not None and parent_id not in parent_ids:
            parent_ids.append(parent_id)
    parent_ids = parent_ids[: settings.retrieval_max_sources]

    parents = {
        chunk.id: chunk
        for chunk in await session.scalars(
            select(Chunk).where(Chunk.document_id == document_id, Chunk.id.in_(parent_ids))
        )
    }
    return [
        Source(sid=index + 1, chunk=parents[parent_id])
        for index, parent_id in enumerate(parent_ids)
    ]
