import os
import structlog
from typing import Optional, AsyncIterator
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.enums import MessageMediaType

from ingestion_service.config import settings
from ingestion_service.parsers.base import BaseParser, ParsedItem

logger = structlog.get_logger()


class TelegramChatParser(BaseParser):
    """
    Parses a private Telegram chat (group or channel) using a user account
    via Pyrogram MTProto client.

    Works with:
    - Private groups you're a member of
    - Private channels you're subscribed to
    - Any chat ID (negative for groups, positive for users/channels)
    """

    def __init__(self, chat_id: str | int, download_dir: str):
        # chat_id can be "@username", numeric ID, or "me"
        self.chat_id = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
        self.download_dir = download_dir
        self.client: Optional[Client] = None

    async def connect(self) -> None:
        session_path = os.path.join(
            os.path.dirname(settings.TELEGRAM_SESSION_FILE),
            os.path.splitext(os.path.basename(settings.TELEGRAM_SESSION_FILE))[0],
        )
        self.client = Client(
            name=session_path,
            api_id=settings.TELEGRAM_API_ID,
            api_hash=settings.TELEGRAM_API_HASH,
            no_updates=True,  # we don't need live updates, only historical
        )
        await self.client.start()
        me = await self.client.get_me()
        logger.info("pyrogram_connected", user=me.username, chat=self.chat_id)

    async def disconnect(self) -> None:
        if self.client:
            await self.client.stop()
            self.client = None

    async def parse(
        self,
        since_message_id: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[ParsedItem]:
        if not self.client:
            raise RuntimeError("Client not connected. Call connect() first.")

        offset_id = 0  # 0 = start from newest
        fetch_limit = limit or 200

        # Pyrogram iter_messages goes newest→oldest by default
        # We use reverse=True to go oldest→newest, offset_id to skip already seen
        kwargs: dict = {
            "chat_id": self.chat_id,
            "limit": fetch_limit,
            "reverse": True,  # oldest first → good for incremental
        }
        if since_message_id:
            kwargs["offset_id"] = since_message_id

        async for message in self.client.get_chat_history(**{
            "chat_id": self.chat_id,
            "limit": fetch_limit,
            "offset_id": since_message_id or 0,
        }):
            item = self._message_to_parsed_item(message)
            if item:
                yield item

    def _message_to_parsed_item(self, message: Message) -> Optional[ParsedItem]:
        item = ParsedItem(
            source_message_id=message.id,
            text=message.text or message.caption or "",
            date=message.date,
            content_type="text",
            raw_metadata={
                "views": getattr(message, "views", None),
                "forwards": getattr(message, "forwards", None),
                "from_user": str(message.from_user.username if message.from_user else None),
            },
        )

        if message.text and not message.media:
            item.content_type = "post"
            return item

        # ── Video ──
        if message.video:
            v = message.video
            item.content_type = "video"
            item.media_type = v.mime_type or "video/mp4"
            item.duration_seconds = float(v.duration or 0)
            item.file_size_bytes = v.file_size
            item.title = v.file_name
            item.raw_metadata["file_id"] = v.file_id
            return item

        # ── Video Note (round video) ──
        if message.video_note:
            vn = message.video_note
            item.content_type = "video"
            item.media_type = "video/mp4"
            item.duration_seconds = float(vn.duration or 0)
            item.file_size_bytes = vn.file_size
            item.raw_metadata["file_id"] = vn.file_id
            item.raw_metadata["is_video_note"] = True
            return item

        # ── Audio ──
        if message.audio:
            a = message.audio
            item.content_type = "audio"
            item.media_type = a.mime_type or "audio/mpeg"
            item.duration_seconds = float(a.duration or 0)
            item.file_size_bytes = a.file_size
            item.title = a.title or a.file_name
            item.raw_metadata["file_id"] = a.file_id
            return item

        # ── Voice Message ──
        if message.voice:
            vc = message.voice
            item.content_type = "audio"
            item.media_type = vc.mime_type or "audio/ogg"
            item.duration_seconds = float(vc.duration or 0)
            item.file_size_bytes = vc.file_size
            item.raw_metadata["file_id"] = vc.file_id
            item.raw_metadata["is_voice"] = True
            return item

        # ── Document (could be video sent as file) ──
        if message.document:
            doc = message.document
            mime = doc.mime_type or ""
            if mime.startswith("video/"):
                item.content_type = "video"
                item.media_type = mime
                item.file_size_bytes = doc.file_size
                item.title = doc.file_name
                item.raw_metadata["file_id"] = doc.file_id
                return item
            elif mime.startswith("audio/"):
                item.content_type = "audio"
                item.media_type = mime
                item.file_size_bytes = doc.file_size
                item.title = doc.file_name
                item.raw_metadata["file_id"] = doc.file_id
                return item

        # Skip photos, stickers, etc.
        return None

    async def download_media(self, item: ParsedItem, output_dir: str) -> str:
        if not self.client:
            raise RuntimeError("Client not connected.")
        if item.source_message_id is None:
            raise ValueError("source_message_id is required for download")

        os.makedirs(output_dir, exist_ok=True)

        # Determine extension from mime type
        ext = _mime_to_ext(item.media_type or "")
        filename = f"msg_{item.source_message_id}{ext}"
        filepath = os.path.join(output_dir, filename)

        # Skip if already downloaded
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            logger.info("download_skipped_exists", path=filepath)
            return filepath

        logger.info("downloading", message_id=item.source_message_id, chat=self.chat_id)

        message = await self.client.get_messages(self.chat_id, item.source_message_id)
        await self.client.download_media(message, file_name=filepath)

        logger.info("download_done", path=filepath, size=os.path.getsize(filepath))
        return filepath


def _mime_to_ext(mime_type: str) -> str:
    mapping = {
        "video/mp4": ".mp4",
        "video/x-matroska": ".mkv",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
        "video/x-msvideo": ".avi",
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
    }
    return mapping.get(mime_type, ".mp4")  # default to mp4 for unknown video