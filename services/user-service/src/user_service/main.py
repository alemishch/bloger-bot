"""User Service — API for user management, onboarding, profiles.

Used by both the Telegram bot and the Mini App frontend.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
import structlog

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("user_service_started")
    yield
    logger.info("user_service_stopped")


app = FastAPI(
    title="Bloger Bot — User Service",
    version="0.1.0",
    lifespan=lifespan,
)

from user_service.routes import router
app.include_router(router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "user-service"}
