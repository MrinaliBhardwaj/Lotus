"""HTTP middleware: request-ID propagation and request body-size caps."""

import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

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


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized request bodies early (CLAUDE.md §2.1 #11).

    File uploads go browser → object storage directly and never pass through
    the API, so this cap only needs to cover JSON bodies. The one exception is
    the local-dev upload route (``exempt_prefixes``), which enforces the much
    larger upload ceiling itself.
    """

    def __init__(self, app: object, max_bytes: int, exempt_prefixes: tuple[str, ...] = ()) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._max_bytes = max_bytes
        self._exempt_prefixes = exempt_prefixes

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        if request.url.path.startswith(self._exempt_prefixes):
            return await call_next(request)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"error": "RequestTooLarge", "detail": "request body too large"},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"error": "BadRequest", "detail": "invalid Content-Length"},
                )
        return await call_next(request)
