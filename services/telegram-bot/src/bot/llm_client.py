"""HTTP client for calling the LLM service."""
import httpx
import structlog
from bot.config import settings

logger = structlog.get_logger()

_timeout = httpx.Timeout(30.0, connect=5.0)


async def ask_llm(query: str, blogger_id: str, chat_history: list[dict] | None = None) -> dict:
    url = f"{settings.LLM_SERVICE_URL}/api/v1/ask"
    payload = {
        "query": query,
        "blogger_id": blogger_id,
        "chat_history": chat_history,
    }
    async with httpx.AsyncClient(timeout=_timeout) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()
