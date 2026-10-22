from fastapi import APIRouter

from app.modules.conversations.routes import router as conversations_router
from app.modules.health.routes import router as health_router

api_router = APIRouter()


api_router.include_router(health_router)
api_router.include_router(conversations_router)
