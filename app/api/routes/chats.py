"""Chat endpoints — thin routers; the ask flow streams SSE (§2.1 pinned)."""

import uuid

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUserId, DbSession, SettingsDep
from app.schemas.chats import ChatCreateRequest, ChatOut, MessageCreateRequest, MessageOut
from app.services import chats as chats_service

router = APIRouter(prefix="/chats", tags=["chats"])

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.post("", response_model=ChatOut, status_code=201)
async def create_chat(
    body: ChatCreateRequest, user_id: CurrentUserId, session: DbSession
) -> ChatOut:
    chat = await chats_service.create_chat(session, user_id, body.document_id)
    return ChatOut.model_validate(chat)


@router.get("", response_model=list[ChatOut])
async def list_chats(user_id: CurrentUserId, session: DbSession) -> list[ChatOut]:
    return [ChatOut.model_validate(c) for c in await chats_service.list_chats(session, user_id)]


@router.get("/{chat_id}/messages", response_model=list[MessageOut])
async def list_messages(
    chat_id: uuid.UUID, user_id: CurrentUserId, session: DbSession
) -> list[MessageOut]:
    messages = await chats_service.list_messages(session, user_id, chat_id)
    return [MessageOut.model_validate(m) for m in messages]


@router.post("/{chat_id}/messages")
async def ask(
    chat_id: uuid.UUID,
    body: MessageCreateRequest,
    user_id: CurrentUserId,
    session: DbSession,
    settings: SettingsDep,
) -> StreamingResponse:
    # ownership/READY/retrieval run BEFORE the stream opens so failures are
    # real HTTP errors; only the LLM stream itself happens inside the response
    chat, sources = await chats_service.prepare_ask(
        session, settings, user_id, chat_id, body.content
    )
    generator = chats_service.stream_answer(session, settings, chat, sources, body.content)
    return StreamingResponse(generator, media_type="text/event-stream", headers=_SSE_HEADERS)
