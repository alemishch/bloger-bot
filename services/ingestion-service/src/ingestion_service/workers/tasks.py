import asyncio
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
    """Step 1: Parse chat → discover content items → save to DB."""

    async def _parse():
        from ingestion_service.parsers.telegram import TelegramChatParser  # ← updated class
        from ingestion_service.services.job_manager import JobManager
        from common.database import get_session_factory
        from common.models.enums import ContentType
        import uuid

        session_factory = get_session_factory()
        async with session_factory() as session:
            jm = JobManager(session)
            source = await jm.get_source(uuid.UUID(source_id))
            if not source:
                raise ValueError(f"Source {source_id} not found")

            chat_id = source.config.get("channel_id") or source.config.get("chat_id")
            if not chat_id:
                raise ValueError("No channel_id/chat_id in source config")

            parser = TelegramChatParser(
                chat_id=chat_id,
                download_dir=settings.DOWNLOAD_DIR,
            )
            await parser.connect()
            count = 0
            max_message_id = source.last_parsed_message_id or 0

            try:
                async for parsed_item in parser.parse(
                    since_message_id=source.last_parsed_message_id,
                    limit=500,
                ):
                    try:
                        ct = ContentType(parsed_item.content_type)
                    except ValueError:
                        ct = ContentType.TEXT

                    item = await jm.upsert_content_item(
                        source_id=source.id,
                        source_message_id=parsed_item.source_message_id,
                        content_type=ct,
                        blogger_id=source.blogger_id,
                        text=parsed_item.text,
                        media_type=parsed_item.media_type,
                        duration_seconds=parsed_item.duration_seconds,
                        file_size_bytes=parsed_item.file_size_bytes,
                        date=parsed_item.date,
                        raw_metadata=parsed_item.raw_metadata,
                        title=parsed_item.title,
                    )
                    count += 1

                    if parsed_item.source_message_id and parsed_item.source_message_id > max_message_id:
                        max_message_id = parsed_item.source_message_id

                    # Only queue downloads for video/audio
                    if ct in (ContentType.VIDEO, ContentType.AUDIO):
                        download_media.delay(str(item.id))

                if max_message_id > (source.last_parsed_message_id or 0):
                    await jm.update_last_parsed_message_id(source.id, max_message_id)

                logger.info("parse_complete", source_id=source_id, count=count)
                return {"parsed_count": count, "last_message_id": max_message_id}
            finally:
                await parser.disconnect()

    return run_async(_parse())


@celery_app.task(bind=True, name="download_media", max_retries=3)
def download_media(self, content_item_id: str):
    """Step 2: Download media file for a content item."""

    async def _download():
        from ingestion_service.parsers.telegram import TelegramChatParser  # ← updated class
        from ingestion_service.parsers.base import ParsedItem
        from ingestion_service.services.job_manager import JobManager
        from common.database import get_session_factory
        from common.models.enums import JobStatus
        import uuid

        session_factory = get_session_factory()
        async with session_factory() as session:
            jm = JobManager(session)
            item = await jm.get_item(uuid.UUID(content_item_id))
            if not item:
                raise ValueError(f"ContentItem {content_item_id} not found")

            source = await jm.get_source(item.source_id)
            chat_id = source.config.get("channel_id") or source.config.get("chat_id")

            await jm.update_item_status(item.id, JobStatus.DOWNLOADING)

            parser = TelegramChatParser(
                chat_id=chat_id,
                download_dir=settings.DOWNLOAD_DIR,
            )

            parsed_item = ParsedItem(
                source_message_id=item.source_message_id,
                content_type=item.content_type.value,
                media_type=item.media_type,
            )

            await parser.connect()
            try:
                filepath = await parser.download_media(parsed_item, settings.DOWNLOAD_DIR)
                await jm.update_item_status(
                    item.id,
                    JobStatus.DOWNLOADED,
                    file_path=filepath,
                )
                logger.info("download_complete", item_id=content_item_id, path=filepath)
                return {"item_id": content_item_id, "file_path": filepath}
            except Exception as e:
                await jm.update_item_status(item.id, JobStatus.DOWNLOAD_FAILED, error_message=str(e))
                raise self.retry(exc=e, countdown=60 * (self.request.retries + 1))
            finally:
                await parser.disconnect()

    return run_async(_download())