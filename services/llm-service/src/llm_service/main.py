from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
import structlog

from llm_service.config import settings
from llm_service.rag import rag_answer

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("llm_service_started", blogger=settings.BLOGGER_ID)
    yield
    logger.info("llm_service_stopped")


app = FastAPI(title="Bloger Bot — LLM Service", version="0.1.0", lifespan=lifespan)


class AskRequest(BaseModel):
    query: str
    blogger_id: Optional[str] = None
    chat_history: Optional[list[dict]] = None


class AskResponse(BaseModel):
    answer: str
    sources: list[dict]
    usage: dict


@app.post("/api/v1/ask", response_model=AskResponse)
async def ask(req: AskRequest):
    blogger = req.blogger_id or settings.BLOGGER_ID
    result = await rag_answer(
        query=req.query,
        blogger_id=blogger,
        chat_history=req.chat_history,
    )
    return result


@app.get("/health")
async def health():
    return {"status": "ok", "service": "llm-service"}
