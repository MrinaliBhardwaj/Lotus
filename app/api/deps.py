"""Request-scoped FastAPI dependencies.

``get_current_user_id`` is the tenancy anchor (hard invariant #7): every
tenant-scoped endpoint takes it, and services filter every query by it. It is
real JWT verification from day one — not a stub — even though the register/
login endpoints only arrive with Task 3.
"""

import uuid
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError
from app.core.security import decode_access_token
from app.db.session import get_db_session

_bearer = HTTPBearer(auto_error=False)

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


async def get_current_user_id(
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> uuid.UUID:
    if credentials is None:
        raise AuthenticationError("missing bearer token")
    return decode_access_token(credentials.credentials, settings)


CurrentUserId = Annotated[uuid.UUID, Depends(get_current_user_id)]
