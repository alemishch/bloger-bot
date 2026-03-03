import os
import structlog
from typing import Optional, AsyncIterator
from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument, DocumentAttributeVideo, DocumentAttributeAudio

from ingestion_service.config import settings
from ingestion_service.parsers.base import BaseParser, ParsedItem

logger = structlog.get_logger()


class TelegramChannelParser(BaseParser):
    def __init__(self, channel_id: str, download_dir: str):
        self.channel_id = channel_id
        self.download_dir = download_dir
        self.client: Optional[TelegramClient] = None

    async def connect(self) -> None:
        self.client = TelegramClient(
            settings.TELEGRAM_SESSION_NAME,
            int(settings.TELEGRAM_API_ID),
            settings.TELEGRAM_API_HASH,
        )
        await self.client.start()
        logger.info("telegram_connected", channel=self.channel_id)

    async def disconnect(self) -> None:
        if self.client:
            await self.client.disconnect()

    async def parse(self, since_message_id: Optional[int] = None, limit: Optional[int] = None) -> AsyncIterator[ParsedItem]:
        if not self.client:
            raise RuntimeError("Client not connected")

        entity = await self.client.get_entity(self.channel_id)
        kwargs = {"entity": entity, "limit": limit or 100}
        if since_message_id:
            kwargs["min_id"] = since_message_id

        async for message in self.client.iter_messages(**kwargs):
            item = ParsedItem(
                source_message_id=message.id,
                text=message.text or "",
                date=message.date,
                content_type="post" if (message.text and not message.media) else "text",
                raw_metadata={"views": getattr(message, "views", None)},
            )

            if message.media and isinstance(message.media, MessageMediaDocument):
                doc = message.media.document
                item.file_size_bytes = doc.size
                item.media_type = doc.mime_type
                for attr in doc.attributes:
                    if isinstance(attr, DocumentAttributeVideo):
                        item.content_type = "video"
                        item.duration_seconds = attr.duration
                    elif isinstance(attr, DocumentAttributeAudio):
                        item.content_type = "audio"
                        item.duration_seconds = attr.duration

            if item.content_type in ("video", "audio", "post", "text"):
                yield item

    async def download_media(self, item: ParsedItem, output_dir: str) -> str:
        if not self.client:
            raise RuntimeError("Client not connected")
        if item.source_message_id is None:
            raise ValueError("source_message_id is required")

        os.makedirs(output_dir, exist_ok=True)
        message = await self.client.get_messages(self.channel_id, ids=item.source_message_id)

        ext = ".bin"
        if item.media_type == "video/mp4":
            ext = ".mp4"
        elif item.media_type and "audio" in item.media_type:
            ext = ".mp3"

        path = os.path.join(output_dir, f"msg_{item.source_message_id}{ext}")
        await self.client.download_media(message, file=path)
        return path