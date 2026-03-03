from contextlib import asynccontextmanager
from fastapi import FastAPI
from ingestion_service.config import settings
from ingestion_service.api.router import api_router
import structlog
import os

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    os.makedirs(settings.DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(settings.TRANSCRIPTION_OUTPUT_DIR, exist_ok=True)
    logger.info("Ingestion service started", environment=settings.ENVIRONMENT)
    yield
    # Shutdown
    logger.info("Ingestion service shutting down")


app = FastAPI(
    title="Bloger Bot - Ingestion Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "ingestion-service"}