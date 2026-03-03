import uuid
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from common.database import get_session
from common.models.enums import JobStatus
from ingestion_service.services.job_manager import JobManager

router = APIRouter()


class ContentItemResponse(BaseModel):
    id: uuid.UUID
    source_message_id: Optional[int]
    content_type: str
    status: str
    title: Optional[str]
    file_path: Optional[str]
    transcript_path: Optional[str]
    error_message: Optional[str]
    blogger_id: str

    class Config:
        from_attributes = True


@router.get("/stats")
async def pipeline_stats(session: AsyncSession = Depends(get_session)):
    """Show how many items are in each pipeline status."""
    jm = JobManager(session)
    return await jm.get_pipeline_stats()


@router.get("/")
async def list_items(
    status: Optional[JobStatus] = Query(None),
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    jm = JobManager(session)
    if status:
        items = await jm.get_items_by_status(status, limit=limit)
    else:
        # Return all recent items
        from sqlalchemy import select
        from common.models.content import ContentItem
        q = select(ContentItem).order_by(ContentItem.created_at.desc()).limit(limit)
        result = await session.execute(q)
        items = list(result.scalars().all())
    return [ContentItemResponse.model_validate(i) for i in items]


@router.post("/{item_id}/retry")
async def retry_item(item_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    jm = JobManager(session)
    item = await jm.get_item(item_id)
    if not item:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Item not found")

    # Reset to appropriate state for retry
    if item.status in (JobStatus.DOWNLOAD_FAILED,):
        await jm.update_item_status(item.id, JobStatus.DISCOVERED)
        from ingestion_service.workers.tasks import download_media
        download_media.delay(str(item.id))
    elif item.status in (JobStatus.TRANSCRIPTION_FAILED,):
        await jm.update_item_status(item.id, JobStatus.DOWNLOADED)

    return {"item_id": str(item_id), "status": "retried"}