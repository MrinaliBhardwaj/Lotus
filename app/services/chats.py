"""Chat lifecycle + the ask flow: retrieve → prompt → stream → validate → persist.

The ask flow streams SSE events: ``sources`` (the resolved source list for
the UI), ``delta`` (validated answer text), ``done`` (persisted message id +
server-resolved citations). Every query is tenant-scoped (invariant 7).
"""

import json
import uuid
from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import deps
from app.core.config import Settings
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.models import Chat, DocumentStatus, Message, MessageRole
from app.services.documents import get_owned_document
from app.services.generation.answer import (
    SYSTEM_PROMPT,
    CitationStreamFilter,
    build_user_prompt,
    resolve_citations,
)
from app.services.retrieval.hybrid import Source, retrieve_sources


def sse_event(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def create_chat(
    session: AsyncSession, user_id: uuid.UUID, document_id: uuid.UUID
) -> Chat:
    document = await get_owned_document(session, user_id, document_id)
    if document.status is not DocumentStatus.READY:
        raise ValidationFailedError(
            f"document is not ready for chat (status: {document.status.value})"
        )
    chat = Chat(user_id=user_id, document_id=document_id, title=document.title)
    session.add(chat)
    await session.commit()
    return chat


async def list_chats(session: AsyncSession, user_id: uuid.UUID) -> list[Chat]:
    result = await session.scalars(
        select(Chat).where(Chat.user_id == user_id).order_by(Chat.created_at.desc())
    )
    return list(result)


async def get_owned_chat(
    session: AsyncSession, user_id: uuid.UUID, chat_id: uuid.UUID
) -> Chat:
    chat = await session.get(Chat, chat_id)
    if chat is None or chat.user_id != user_id:
        raise NotFoundError("chat not found")  # 404, never 403 (invariant 7)
    return chat


async def list_messages(
    session: AsyncSession, user_id: uuid.UUID, chat_id: uuid.UUID
) -> list[Message]:
    chat = await get_owned_chat(session, user_id, chat_id)
    result = await session.scalars(
        select(Message).where(Message.chat_id == chat.id).order_by(Message.created_at)
    )
    return list(result)


async def prepare_ask(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    chat_id: uuid.UUID,
    question: str,
) -> tuple[Chat, list[Source]]:
    """Everything that can fail with a clean HTTP error happens HERE, before
    the streaming response starts (a started stream can't change its status)."""
    chat = await get_owned_chat(session, user_id, chat_id)
    sources = await retrieve_sources(session, settings, user_id, chat.document_id, question)
    session.add(Message(chat_id=chat.id, role=MessageRole.USER, content=question))
    await session.commit()
    return chat, sources


async def stream_answer(
    session: AsyncSession,
    settings: Settings,
    chat: Chat,
    sources: list[Source],
    question: str,
) -> AsyncIterator[str]:
    """SSE generator for one question; persists the assistant message (with
    server-resolved citations) after the stream ends."""
    yield sse_event(
        "sources",
        {
            "sources": [
                {
                    "sid": s.sid,
                    "page_start": s.chunk.page_start,
                    "page_end": s.chunk.page_end,
                    "section_path": s.chunk.section_path,
                    "section_title": s.chunk.section_title,
                }
                for s in sources
            ]
        },
    )

    llm = deps.get_llm_provider(settings)
    citation_filter = CitationStreamFilter({s.sid for s in sources})
    answer_parts: list[str] = []
    async for raw_chunk in llm.stream(
        system=SYSTEM_PROMPT,
        user=build_user_prompt(question, sources),
        max_tokens=settings.llm_max_tokens,
    ):
        validated = citation_filter.feed(raw_chunk)
        if validated:
            answer_parts.append(validated)
            yield sse_event("delta", {"text": validated})
    tail = citation_filter.flush()
    if tail:
        answer_parts.append(tail)
        yield sse_event("delta", {"text": tail})

    citations = resolve_citations(citation_filter.used_sids, sources)
    message = Message(
        chat_id=chat.id,
        role=MessageRole.ASSISTANT,
        content="".join(answer_parts),
        citations=citations,
        token_usage={"model": llm.model},
    )
    session.add(message)
    await session.commit()
    yield sse_event("done", {"message_id": str(message.id), "citations": citations})
