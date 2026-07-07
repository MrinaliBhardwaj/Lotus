from fastapi import APIRouter

from app.api.deps import SettingsDep
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(settings: SettingsDep) -> HealthResponse:
    """Liveness probe — deliberately dependency-free (no DB/Redis round trips)."""
    return HealthResponse(status="ok", app=settings.app_name, env=settings.app_env)
