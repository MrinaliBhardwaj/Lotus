import uuid

import pytest

from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("s3cret-password")
    assert hashed != "s3cret-password"
    assert hashed.startswith("$argon2id$")  # pinned algorithm (§2.1 #1)
    assert verify_password("s3cret-password", hashed)
    assert not verify_password("wrong-password", hashed)


def test_jwt_roundtrip(settings: Settings, user_id: uuid.UUID) -> None:
    token = create_access_token(user_id, settings)
    assert decode_access_token(token, settings) == user_id


def test_jwt_rejects_garbage(settings: Settings) -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token("not-a-token", settings)


def test_jwt_rejects_wrong_key(settings: Settings, user_id: uuid.UUID) -> None:
    other = settings.model_copy(
        update={"jwt_secret_key": "different-secret-0123456789abcdef01234567"}
    )
    token = create_access_token(user_id, other)
    with pytest.raises(AuthenticationError):
        decode_access_token(token, settings)


async def test_protected_dependency_rejects_missing_token() -> None:
    # get_current_user_id is the tenancy anchor — verify it fails closed.
    from app.api.deps import get_current_user_id

    with pytest.raises(AuthenticationError):
        await get_current_user_id(Settings(app_env="test"), None)
