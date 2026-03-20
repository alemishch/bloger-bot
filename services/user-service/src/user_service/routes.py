"""User Service REST API — consumed by bot and Mini App."""
import uuid
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from user_service.db import get_session

router = APIRouter()


# ── Schemas ─────────────────────────────────────────────────────────────────

class UserProfile(BaseModel):
    id: str
    telegram_id: int
    blogger_id: str
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    onboarding_status: str
    onboarding_step: Optional[str] = None
    profile_data: Optional[dict] = None
    long_term_profile: Optional[dict] = None
    created_at: Optional[str] = None


class OnboardingResponseOut(BaseModel):
    step_id: str
    question_text: str
    answer_value: str
    created_at: Optional[str] = None


class ChatMessageOut(BaseModel):
    id: str
    role: str
    content: str
    token_count: Optional[int] = None
    created_at: Optional[str] = None


class ChatSessionOut(BaseModel):
    id: str
    is_active: bool
    started_at: Optional[str] = None
    last_message_at: Optional[str] = None
    message_count: int = 0


# ── User endpoints ──────────────────────────────────────────────────────────

@router.get("/users/{telegram_id}", response_model=UserProfile)
async def get_user(telegram_id: int, session: AsyncSession = Depends(get_session)):
    row = (await session.execute(
        text("""SELECT id, telegram_id, blogger_id::text, username, first_name, last_name,
                       phone, email, onboarding_status::text, onboarding_step,
                       profile_data, long_term_profile, created_at
                FROM users WHERE telegram_id = :tid"""),
        {"tid": telegram_id},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return {**dict(row), "id": str(row["id"]), "created_at": str(row["created_at"]) if row["created_at"] else None}


@router.get("/users/{telegram_id}/onboarding", response_model=list[OnboardingResponseOut])
async def get_onboarding(telegram_id: int, session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        text("""SELECT r.step_id, r.question_text, r.answer_value, r.created_at
                FROM onboarding_responses r
                JOIN users u ON r.user_id = u.id
                WHERE u.telegram_id = :tid ORDER BY r.created_at"""),
        {"tid": telegram_id},
    )).mappings().all()
    return [
        {**dict(r), "created_at": str(r["created_at"]) if r["created_at"] else None}
        for r in rows
    ]


# ── Session / Message endpoints (for Mini App chat UI) ──────────────────────

@router.get("/users/{telegram_id}/sessions", response_model=list[ChatSessionOut])
async def get_sessions(telegram_id: int, limit: int = Query(10, le=50), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        text("""SELECT s.id, s.is_active, s.started_at, s.last_message_at,
                       (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) as message_count
                FROM chat_sessions s
                JOIN users u ON s.user_id = u.id
                WHERE u.telegram_id = :tid
                ORDER BY s.started_at DESC LIMIT :lim"""),
        {"tid": telegram_id, "lim": limit},
    )).mappings().all()
    return [
        {**dict(r), "id": str(r["id"]),
         "started_at": str(r["started_at"]) if r["started_at"] else None,
         "last_message_at": str(r["last_message_at"]) if r["last_message_at"] else None}
        for r in rows
    ]


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageOut])
async def get_messages(session_id: str, session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        text("""SELECT id, role, content, token_count, created_at
                FROM chat_messages WHERE session_id = CAST(:sid AS uuid)
                ORDER BY created_at"""),
        {"sid": session_id},
    )).mappings().all()
    return [
        {**dict(r), "id": str(r["id"]),
         "created_at": str(r["created_at"]) if r["created_at"] else None}
        for r in rows
    ]
