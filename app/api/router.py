"""Top-level router aggregation. Feature routers register here as tasks land."""

from fastapi import APIRouter

from app.api.routes import auth, chats, documents, health, local_uploads

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(documents.router)
api_router.include_router(local_uploads.router)
api_router.include_router(chats.router)
