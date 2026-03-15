import os
import structlog
from typing import Optional, AsyncIterator
from pyrogram import Client
from pyrogram.types import Message

from ingestion_service.config import settings
from ingestion_service.parsers.base import BaseParser, ParsedItem

logger = structlog.get_logger()


class TelegramChatParser(BaseParser):

    def __init__(self, chat_id: str | int, download_dir: str):
        self.chat_id = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
        self.download_dir = download_dir
        self.client: Optional[Client] = None

    async def connect(self) -> None:
        sessions_dir = settings.SESSIONS_DIR
        session_name = settings.TELEGRAM_SESSION_NAME

        # Pyrogram looks for: {workdir}/{name}.session
        expected_path = os.path.join(sessions_dir, f"{session_name}.session")

        logger.info(
            "telegram_connect_attempt",
            sessions_dir=sessions_dir,
            session_name=session_name,
            expected_path=expected_path,
            exists=os.path.exists(expected_path),
            dir_contents=os.listdir(sessions_dir) if os.path.exists(sessions_dir) else "DIR_NOT_FOUND",
        )

        if not os.path.exists(expected_path):
            raise RuntimeError(
                f"Session file not found: {expected_path}\n"
                f"Sessions dir contents: {os.listdir(sessions_dir) if os.path.exists(sessions_dir) else 'directory does not exist'}\n"
                f"Create it with: python tools/create_session.py\n"
                f"Then ensure it's at: sessions/{session_name}.session"
            )

        self.client = Client(
            name=session_name,
            api_id=int(settings.TELEGRAM_API_ID),
            api_hash=settings.TELEGRAM_API_HASH,
            workdir=sessions_dir,
            # CRITICAL: prevent interactive login prompt in non-interactive environments
            in_memory=False,
            no_updates=True,
        )
        await self.client.start()
        me = await self.client.get_me()
        logger.info("pyrogram_connected", user=me.username, chat_id=self.chat_id)

    async def disconnect(self) -> None:
        if self.client:
            try:
                await self.client.stop()
            except Exception:
                pass
            self.client = None

    async def parse(
        self,
        since_message_id: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[ParsedItem]:
        if not self.client:
            raise RuntimeError("Client not connected. Call connect() first.")

        kwargs = {
            "chat_id": self.chat_id,
            "limit": limit or 500,
        }
        # offset_id: Pyrogram returns messages with ID < offset_id
        # So to get messages AFTER since_message_id, we DON'T use offset_id —
        # we fetch all and filter. For large chats, use offset_id=0 (latest).
        # For incremental: fetch newest batch, yield only those newer than last seen.
        
        messages = []
        async for message in self.client.get_chat_history(**kwargs):
            # get_chat_history goes newest → oldest
            # Stop early if we've already seen this message
            if since_message_id and message.id <= since_message_id:
                break
            messages.append(message)

        # Yield oldest → newest (correct chronological order for pipeline)
        for message in reversed(messages):
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

        if not message.media:
            if message.text:
                item.content_type = "post"
                return item
            return None  # skip empty

        if message.video:
            v = message.video
            item.content_type = "video"
            item.media_type = v.mime_type or "video/mp4"
            item.duration_seconds = float(v.duration or 0)
            item.file_size_bytes = v.file_size
            item.title = v.file_name
            item.raw_metadata["file_id"] = v.file_id
            return item

        if message.video_note:
            vn = message.video_note
            item.content_type = "video"
            item.media_type = "video/mp4"
            item.duration_seconds = float(vn.duration or 0)
            item.file_size_bytes = vn.file_size
            item.raw_metadata["file_id"] = vn.file_id
            item.raw_metadata["is_video_note"] = True
            return item

        if message.audio:
            a = message.audio
            item.content_type = "audio"
            item.media_type = a.mime_type or "audio/mpeg"
            item.duration_seconds = float(a.duration or 0)
            item.file_size_bytes = a.file_size
            item.title = a.title or a.file_name
            item.raw_metadata["file_id"] = a.file_id
            return item

        if message.voice:
            vc = message.voice
            item.content_type = "audio"
            item.media_type = vc.mime_type or "audio/ogg"
            item.duration_seconds = float(vc.duration or 0)
            item.file_size_bytes = vc.file_size
            item.raw_metadata["file_id"] = vc.file_id
            item.raw_metadata["is_voice"] = True
            return item

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
            if mime.startswith("audio/"):
                item.content_type = "audio"
                item.media_type = mime
                item.file_size_bytes = doc.file_size
                item.title = doc.file_name
                item.raw_metadata["file_id"] = doc.file_id
                return item

        return None

    async def download_media(self, item: ParsedItem, output_dir: str) -> str:
        if not self.client:
            raise RuntimeError("Client not connected.")
        if item.source_message_id is None:
            raise ValueError("source_message_id is required for download")

        os.makedirs(output_dir, exist_ok=True)
        ext = _mime_to_ext(item.media_type or "")
        filename = f"msg_{item.source_message_id}{ext}"
        filepath = os.path.join(output_dir, filename)

        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            logger.info("download_skipped_exists", path=filepath)
            return filepath

        logger.info("downloading", message_id=item.source_message_id, chat=self.chat_id)
        message = await self.client.get_messages(self.chat_id, item.source_message_id)
        await self.client.download_media(message, file_name=filepath)
        logger.info("download_done", path=filepath, size=os.path.getsize(filepath))
        return filepath


def _mime_to_ext(mime_type: str) -> str:
    return {
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
    }.get(mime_type, ".mp4")