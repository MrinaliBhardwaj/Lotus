"""HTTP middleware: request-ID propagation and request body-size caps."""

import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import request_id_var

CallNext = Callable[[Request], Awaitable[Response]]


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Accept an inbound X-Request-ID or mint one; echo it on the response and
    seed the logging contextvar so every log line in the request carries it."""

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response


class _BodyTooLarge(Exception):
    """Raised from the wrapped receive channel once the cap is exceeded."""


class BodySizeLimitMiddleware:
    """Reject oversized request bodies (CLAUDE.md §2.1 #11).

    Pure ASGI so the cap is enforced on the *actual* bytes streamed in, not on
    the ``Content-Length`` header — a chunked request (or one that lies about
    its length) carries no reliable length, so a header-only check is
    bypassable and would let an **unauthenticated** caller buffer an unbounded
    body into memory before auth ever runs. We fast-reject an oversized
    declared length, then tally bytes as they arrive and abort past the cap.

    File uploads go browser → object storage directly and never pass through
    the API, so this only needs to cover JSON bodies. The one exception is the
    local-dev upload route (``exempt_prefixes``), which streams and enforces
    the much larger upload ceiling itself.
    """

    def __init__(
        self, app: ASGIApp, max_bytes: int, exempt_prefixes: tuple[str, ...] = ()
    ) -> None:
        self._app = app
        self._max_bytes = max_bytes
        self._exempt_prefixes = exempt_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(self._exempt_prefixes):
            await self._app(scope, receive, send)
            return

        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    if int(value) > self._max_bytes:
                        await self._reject(send, 413, "request body too large")
                        return
                except ValueError:
                    await self._reject(send, 400, "invalid Content-Length")
                    return
                break

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    raise _BodyTooLarge
            return message

        response_started = False

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, counting_receive, guarded_send)
        except _BodyTooLarge:
            # The body was never fully buffered, so the OOM is already averted.
            # Send a clean 413 when the app hasn't started responding (the
            # normal case for a body-reading endpoint); otherwise the truncated
            # connection simply errors out.
            if not response_started:
                await self._reject(send, 413, "request body too large")

    async def _reject(self, send: Send, status: int, detail: str) -> None:
        error = "RequestTooLarge" if status == 413 else "BadRequest"
        response = JSONResponse(status_code=status, content={"error": error, "detail": detail})
        await response({"type": "http"}, _empty_receive, send)


async def _empty_receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}
