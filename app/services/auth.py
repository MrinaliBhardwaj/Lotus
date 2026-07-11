"""Register/login (CLAUDE.md §2.1 #1: argon2id + JWT HS256, 60-min expiry)."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, ConflictError
from app.core.security import create_access_token, hash_password, verify_password
from app.models import User

# Verified against when the email doesn't exist, so login latency doesn't
# reveal whether an account exists (user-enumeration timing oracle).
_dummy_hash: str | None = None


def _get_dummy_hash() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("timing-equalizer-not-a-real-password")
    return _dummy_hash


async def register_user(
    session: AsyncSession, settings: Settings, email: str, password: str
) -> tuple[User, str]:
    user = User(email=email.strip().lower(), hashed_pw=hash_password(password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError("an account with this email already exists") from None
    return user, create_access_token(user.id, settings)


async def login_user(
    session: AsyncSession, settings: Settings, email: str, password: str
) -> tuple[User, str]:
    user = await session.scalar(select(User).where(User.email == email.strip().lower()))
    hashed = user.hashed_pw if user is not None else _get_dummy_hash()
    if not verify_password(password, hashed) or user is None:
        raise AuthenticationError("invalid email or password")
    return user, create_access_token(user.id, settings)
