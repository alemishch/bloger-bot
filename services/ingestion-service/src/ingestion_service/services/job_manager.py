import uuid
import structlog
from datetime import datetime
from typing import Optional
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.content import ContentSource, ContentItem
from common.models.enums import JobStatus, ContentType, SourceType, BloggerID

logger = structlog.get_logger()


class JobManager:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ── Sources ──

    async def create_source(self, name: str, source_type: SourceType, blogger_id: BloggerID, config: dict) -> ContentSource:
        source = ContentSource(name=name, source_type=source_type, blogger_id=blogger_id, config=config)
        self.session.add(source)
        await self.session.commit()
        await self.session.refresh(source)
        logger.info("source_created", source_id=str(source.id), name=name)
        return source

    async def get_source(self, source_id: uuid.UUID) -> Optional[ContentSource]:
        return await self.session.get(ContentSource, source_id)

    async def list_sources(self, active_only: bool = True) -> list[ContentSource]:
        q = select(ContentSource)
        if active_only:
            q = q.where(ContentSource.is_active == True)
        result = await self.session.execute(q)
        return list(result.scalars().all())

    async def update_last_parsed_message_id(self, source_id: uuid.UUID, message_id: int):
        await self.session.execute(
            update(ContentSource)
            .where(ContentSource.id == source_id)
            .values(last_parsed_message_id=message_id, updated_at=datetime.utcnow())
        )
        await self.session.commit()

    # ── Content Items ──

    async def upsert_content_item(self, source_id: uuid.UUID, source_message_id: int, content_type: ContentType, blogger_id: BloggerID, **kwargs) -> ContentItem:
        q = select(ContentItem).where(
            ContentItem.source_id == source_id,
            ContentItem.source_message_id == source_message_id,
        )
        result = await self.session.execute(q)
        existing = result.scalar_one_or_none()

        if existing:
            logger.debug("content_item_exists", message_id=source_message_id)
            return existing

        item = ContentItem(
            source_id=source_id,
            source_message_id=source_message_id,
            content_type=content_type,
            blogger_id=blogger_id,
            status=JobStatus.DISCOVERED,
            **kwargs,
        )
        self.session.add(item)
        await self.session.commit()
        await self.session.refresh(item)
        logger.info("content_item_created", item_id=str(item.id), message_id=source_message_id)
        return item

    async def update_item_status(self, item_id: uuid.UUID, status: JobStatus, error_message: Optional[str] = None, **extra_fields):
        values = {"status": status, "updated_at": datetime.utcnow()}
        if error_message:
            values["error_message"] = error_message
        values.update(extra_fields)
        await self.session.execute(
            update(ContentItem).where(ContentItem.id == item_id).values(**values)
        )
        await self.session.commit()

    async def get_items_by_status(self, status: JobStatus, limit: int = 50) -> list[ContentItem]:
        q = (
            select(ContentItem)
            .where(ContentItem.status == status)
            .order_by(ContentItem.created_at)
            .limit(limit)
        )
        result = await self.session.execute(q)
        return list(result.scalars().all())

    async def get_item(self, item_id: uuid.UUID) -> Optional[ContentItem]:
        return await self.session.get(ContentItem, item_id)

    async def get_pipeline_stats(self) -> dict:
        """Single-query aggregation using raw SQL to avoid enum deserialization errors."""
        from sqlalchemy import text

        # Initialize all statuses to 0
        stats = {s.value: 0 for s in JobStatus}

        # Use raw SQL with ::text cast — bypasses SQLAlchemy enum deserialization entirely
        result = await self.session.execute(
            text("SELECT status::text, COUNT(id) AS cnt FROM content_items GROUP BY status")
        )
        for row in result.fetchall():
            key = row[0].lower()  # normalize to lowercase just in case
            if key in stats:
                stats[key] = row[1]
            else:
                stats[key] = row[1]  # include unknown values too

        return stats