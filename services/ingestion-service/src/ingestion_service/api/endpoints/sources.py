import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from common.database import get_session
from common.models.enums import SourceType, BloggerID
from ingestion_service.services.job_manager import JobManager

router = APIRouter()


class CreateSourceRequest(BaseModel):
    name: str
    source_type: SourceType
    blogger_id: BloggerID
    config: dict  # e.g. {"channel_id": "@yuri_channel"}


class SourceResponse(BaseModel):
    id: uuid.UUID
    name: str
    source_type: str
    blogger_id: str
    is_active: bool
    last_parsed_message_id: Optional[int]
    config: dict = Field(default_factory=dict)

    @field_validator("config", mode="before")
    @classmethod
    def _config_dict(cls, v):
        return v if isinstance(v, dict) else {}

    class Config:
        from_attributes = True


@router.post("/", response_model=SourceResponse)
async def create_source(request: CreateSourceRequest, session: AsyncSession = Depends(get_session)):
    jm = JobManager(session)
    source = await jm.create_source(
        name=request.name,
        source_type=request.source_type,
        blogger_id=request.blogger_id,
        config=request.config,
    )
    return source


@router.get("/")
async def list_sources(session: AsyncSession = Depends(get_session)):
    jm = JobManager(session)
    sources = await jm.list_sources()
    return [SourceResponse.model_validate(s) for s in sources]


@router.post("/{source_id}/parse")
async def trigger_parse(source_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    jm = JobManager(session)
    source = await jm.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    from ingestion_service.workers.tasks import parse_telegram_channel
    task = parse_telegram_channel.delay(str(source_id))
    return {"task_id": task.id, "source_id": str(source_id), "status": "queued"}


@router.post("/{source_id}/parse-text")
async def trigger_parse_text(
    source_id: uuid.UUID,
    max_messages: int = 0,
    session: AsyncSession = Depends(get_session),
):
    """Parse only text posts from a channel (background task). max_messages=0 for all."""
    jm = JobManager(session)
    source = await jm.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    from ingestion_service.workers.tasks import parse_channel_text
    task = parse_channel_text.delay(str(source_id), max_messages=max_messages)
    return {"task_id": task.id, "source_id": str(source_id), "status": "queued_background"}


@router.post("/cancel-task/{task_id}")
async def cancel_task(task_id: str):
    """Cancel a running background parse task."""
    from ingestion_service.workers.celery_app import celery_app
    celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
    return {"task_id": task_id, "status": "cancel_requested"}