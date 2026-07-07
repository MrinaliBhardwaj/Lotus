"""Explicit application exception types (CLAUDE.md §3: no bare except).

Every exception here maps to exactly one HTTP response in
:mod:`app.core.error_handlers`. Services raise these; routers never construct
HTTP errors themselves.
"""


class LexaError(Exception):
    """Base class for all application errors."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class NotFoundError(LexaError):
    """Requested resource does not exist or is not visible to this tenant."""


class AuthenticationError(LexaError):
    """Missing or invalid credentials."""


class PermissionDeniedError(LexaError):
    """Authenticated, but not allowed to act on this resource."""


class ValidationFailedError(LexaError):
    """Domain-level validation failure (e.g. rejected file, bad state transition)."""


class ConflictError(LexaError):
    """Resource already exists (e.g. per-user duplicate document hash)."""


class StorageError(LexaError):
    """Object-storage operation failed."""


class ProviderError(LexaError):
    """LLM/embedding provider call failed."""
