"""Auth endpoints — thin routers, logic lives in services/auth.py (§3)."""

from fastapi import APIRouter, Depends

from app.api.deps import DbSession, SettingsDep, auth_rate_limit
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(auth_rate_limit)])


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(
    body: RegisterRequest, session: DbSession, settings: SettingsDep
) -> TokenResponse:
    user, token = await auth_service.register_user(session, settings, body.email, body.password)
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session: DbSession, settings: SettingsDep) -> TokenResponse:
    user, token = await auth_service.login_user(session, settings, body.email, body.password)
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))
