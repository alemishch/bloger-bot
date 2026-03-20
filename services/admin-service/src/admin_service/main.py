"""Admin Service — dashboard, user management, analytics, content export."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import make_asgi_app
import structlog

logger = structlog.get_logger()

SRC = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("admin_service_started")
    yield
    logger.info("admin_service_stopped")


app = FastAPI(title="Bloger Bot — Admin Panel", version="0.1.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=SRC / "static"), name="static")

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

templates = Jinja2Templates(directory=SRC / "templates")

from admin_service.routes import router as api_router
from admin_service.views import router as views_router

app.include_router(api_router, prefix="/api/v1")
app.include_router(views_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "admin-service"}
