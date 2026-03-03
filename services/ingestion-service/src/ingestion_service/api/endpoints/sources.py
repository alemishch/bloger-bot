import uuid
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class CreateSourceRequest(BaseModel):
    name: str
    source_type: str
    config: dict
    blogger_id: str


class SourceResponse(BaseModel):
    id: uuid.UUID
    name: str
    source_type: str
    blogger_id: str
    is_active: bool


@router.post("/", response_model=SourceResponse)
async def create_source(request: CreateSourceRequest):
    return SourceResponse(
        id=uuid.uuid4(),
        name=request.name,
        source_type=request.source_type,
        blogger_id=request.blogger_id,
        is_active=True,
    )


@router.get("/")
async def list_sources():
    return []


@router.post("/{source_id}/parse")
async def trigger_parse(source_id: uuid.UUID):
    from ingestion_service.workers.tasks import parse_telegram_channel
    task = parse_telegram_channel.delay(str(source_id))
    return {"task_id": task.id, "source_id": str(source_id), "status": "queued"}