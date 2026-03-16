import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    String, Text, DateTime, JSON, Boolean, Integer, BigInteger,
    ForeignKey, Index, Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from common.models.base import Base
from common.models.enums import BloggerID, OnboardingStatus


def _enum_values(enum_cls):
    return [e.value for e in enum_cls]


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    blogger_id: Mapped[BloggerID] = mapped_column(
        SAEnum(BloggerID, values_callable=_enum_values, name="bloggerid", create_type=False),
        nullable=False,
    )
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    onboarding_status: Mapped[OnboardingStatus] = mapped_column(
        SAEnum(OnboardingStatus, values_callable=_enum_values, name="onboardingstatus"),
        default=OnboardingStatus.NOT_STARTED,
    )
    onboarding_step: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    profile_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    long_term_profile: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    amocrm_contact_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sessions: Mapped[list["ChatSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    onboarding_responses: Mapped[list["OnboardingResponse"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )


class ChatSession(Base):
    """A continuous dialogue. Closes after 2h of inactivity (per spec §14.2)."""
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    blogger_id: Mapped[BloggerID] = mapped_column(
        SAEnum(BloggerID, values_callable=_enum_values, name="bloggerid", create_type=False),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_message_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="sessions")
    messages: Mapped[list["ChatMessage"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_session_created", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_sessions.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)
    token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["ChatSession"] = relationship(back_populates="messages")


class OnboardingResponse(Base):
    """Stores each answer given during the onboarding questionnaire."""
    __tablename__ = "onboarding_responses"
    __table_args__ = (
        Index("ix_onboarding_user_step", "user_id", "step_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    blogger_id: Mapped[BloggerID] = mapped_column(
        SAEnum(BloggerID, values_callable=_enum_values, name="bloggerid", create_type=False),
        nullable=False,
    )
    step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_value: Mapped[str] = mapped_column(Text, nullable=False)
    answer_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="onboarding_responses")
