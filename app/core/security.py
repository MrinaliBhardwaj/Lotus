"""Auth primitives (pinned in CLAUDE.md §2.1 #1).

Password hashing: argon2id (argon2-cffi defaults).
Tokens: JWT HS256, 60-minute expiry, no refresh tokens in Phase 1.

The register/login endpoints arrive with Task 3 (they need the users table from
Task 2); these primitives exist now so every later endpoint is built against a
real ``get_current_user`` dependency instead of a stub that gets retrofitted.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from app.core.config import Settings
from app.core.exceptions import AuthenticationError

_hasher = PasswordHasher()  # argon2id with library-recommended parameters


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError):
        return False


def create_access_token(user_id: uuid.UUID, settings: Settings) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, settings: Settings) -> uuid.UUID:
    """Return the authenticated user_id — the value every tenant-scoped query filters by."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp"]},
        )
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("invalid or expired token") from exc
    try:
        return uuid.UUID(claims["sub"])
    except ValueError as exc:
        raise AuthenticationError("malformed token subject") from exc
