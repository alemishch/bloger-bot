import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Text, Integer, Float, DateTime, ForeignKey, JSON, Enum, Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from common.models.base import Base
from common.models.enums import ContentType, SourceType, JobStatus, BloggerID


class ContentSource(Base):
    __tablename__ = "content_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType), nullable=False)
    blogger_id: Mapped[BloggerID] = mapped_column(Enum(BloggerID), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # channel_id, url, etc.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_parsed_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items: Mapped[list["ContentItem"]] = relationship(back_populates="source", cascade="all, delete-orphan")


class ContentItem(Base):
    __tablename__ = "content_items"
    __table_args__ = (
        Index("ix_content_items_source_message", "source_id", "source_message_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content_sources.id"), nullable=False)
    source_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    content_type: Mapped[ContentType] = mapped_column(Enum(ContentType), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    media_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    file_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    transcript_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    transcript_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # LLM labeling results
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    themes: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    problems_solved: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    tools_mentioned: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    target_audience: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    content_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # serious/humor/metaphor
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    label_metadata: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Vectorization
    chroma_collection: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    chunk_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.DISCOVERED)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    blogger_id: Mapped[BloggerID] = mapped_column(Enum(BloggerID), nullable=False)
    date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    raw_metadata: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    source: Mapped["ContentSource"] = relationship(back_populates="items")
    chunks: Mapped[list["ContentChunk"]] = relationship(back_populates="content_item", cascade="all, delete-orphan")


class ContentChunk(Base):
    __tablename__ = "content_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content_items.id"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # for video/audio
    end_time: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chroma_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    content_item: Mapped["ContentItem"] = relationship(back_populates="chunks")