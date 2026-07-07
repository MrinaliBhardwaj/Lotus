"""The single mapping from application exceptions to HTTP responses."""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    LexaError,
    NotFoundError,
    PermissionDeniedError,
    ProviderError,
    StorageError,
    ValidationFailedError,
)
from app.core.logging import request_id_var

logger = logging.getLogger(__name__)

_STATUS_BY_EXC: dict[type[LexaError], int] = {
    AuthenticationError: 401,
    PermissionDeniedError: 403,
    NotFoundError: 404,
    ConflictError: 409,
    ValidationFailedError: 422,
    StorageError: 502,
    ProviderError: 502,
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LexaError)
    async def _handle_lexa_error(request: Request, exc: LexaError) -> JSONResponse:
        status = _STATUS_BY_EXC.get(type(exc), 500)
        if status >= 500:
            logger.error("unhandled domain error: %s", exc.message, exc_info=exc)
        body: dict[str, str | None] = {
            "error": type(exc).__name__,
            "detail": exc.message,
            "request_id": request_id_var.get(),
        }
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse(status_code=status, content=body, headers=headers)
