from fastapi import APIRouter
from ingestion_service.api.endpoints import jobs, sources

api_router = APIRouter()
api_router.include_router(sources.router, prefix="/sources", tags=["sources"])
api_router.include_router(jobs.router, prefix="/jobs", tags=["jobs"])