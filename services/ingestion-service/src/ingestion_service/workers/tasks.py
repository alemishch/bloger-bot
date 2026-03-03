import asyncio
import json
import os
import structlog
from ingestion_service.workers.celery_app import celery_app
from ingestion_service.config import settings

logger = structlog.get_logger()


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, name="parse_telegram_channel", max_retries=3)
def parse_telegram_channel(self, source_id: str):
    async def _parse():
        from ingestion_service.parsers.telegram import TelegramChannelParser

        channels = settings.source_channels
        if not channels:
            raise ValueError("No Telegram channels configured")

        parser = TelegramChannelParser(channel_id=channels[0], download_dir=settings.DOWNLOAD_DIR)
        await parser.connect()
        count = 0
        try:
            async for item in parser.parse(limit=50):
                count += 1
                if item.content_type in ("video", "audio"):
                    download_and_process.delay(
                        source_id=source_id,
                        channel_id=channels[0],
                        message_id=item.source_message_id,
                        content_type=item.content_type,
                        media_type=item.media_type,
                        text=item.text,
                        duration=item.duration_seconds,
                        metadata=item.raw_metadata,
                    )
            return {"parsed_count": count}
        finally:
            await parser.disconnect()

    return run_async(_parse())


@celery_app.task(bind=True, name="download_and_process", max_retries=3)
def download_and_process(
    self,
    source_id: str,
    channel_id: str,
    message_id: int,
    content_type: str,
    media_type: str | None,
    text: str | None,
    duration: float | None,
    metadata: dict,
):
    async def _download():
        from ingestion_service.parsers.telegram import TelegramChannelParser
        from ingestion_service.parsers.base import ParsedItem

        parser = TelegramChannelParser(channel_id=channel_id, download_dir=settings.DOWNLOAD_DIR)
        item = ParsedItem(
            source_message_id=message_id,
            content_type=content_type,
            media_type=media_type,
            text=text,
            duration_seconds=duration,
            raw_metadata=metadata,
        )

        await parser.connect()
        try:
            filepath = await parser.download_media(item, settings.DOWNLOAD_DIR)
            os.makedirs(settings.DOWNLOAD_DIR, exist_ok=True)
            job_path = os.path.join(settings.DOWNLOAD_DIR, f"job_{message_id}.json")
            with open(job_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "source_id": source_id,
                        "message_id": message_id,
                        "content_type": content_type,
                        "file_path": filepath,
                        "status": "downloaded",
                        "metadata": metadata,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            return {"message_id": message_id, "file_path": filepath}
        finally:
            await parser.disconnect()

    return run_async(_download())