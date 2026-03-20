"""Admin HTML views — server-rendered dashboard pages."""
from pathlib import Path
from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from admin_service.db import get_session

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@router.get("/")
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    stats = {}
    for q, key in [
        ("SELECT COUNT(*) FROM users", "total_users"),
        ("SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '24 hours'", "new_users_24h"),
        ("SELECT COUNT(*) FROM users WHERE onboarding_status = 'completed'::onboardingstatus", "onboarded"),
        ("SELECT COUNT(*) FROM chat_sessions", "total_sessions"),
        ("SELECT COUNT(*) FROM chat_messages", "total_messages"),
        ("SELECT COUNT(*) FROM chat_messages WHERE created_at > NOW() - INTERVAL '24 hours'", "messages_24h"),
    ]:
        stats[key] = (await session.execute(text(q))).scalar() or 0

    pipeline = await session.execute(
        text("SELECT status::text, COUNT(*) FROM content_items GROUP BY status ORDER BY COUNT(*) DESC")
    )
    stats["pipeline"] = {row[0]: row[1] for row in pipeline.fetchall()}
    stats["total_content"] = sum(stats["pipeline"].values())

    return templates.TemplateResponse("dashboard.html", {"request": request, "stats": stats})


@router.get("/users")
async def users_page(request: Request, session: AsyncSession = Depends(get_session)):
    r = await session.execute(text("""
        SELECT u.id, u.telegram_id, u.blogger_id::text, u.username, u.first_name,
               u.onboarding_status::text, u.created_at,
               (SELECT COUNT(*) FROM chat_messages cm
                JOIN chat_sessions cs ON cm.session_id = cs.id
                WHERE cs.user_id = u.id) as msg_count
        FROM users u ORDER BY u.created_at DESC LIMIT 100
    """))
    users = [dict(row._mapping) for row in r.fetchall()]
    return templates.TemplateResponse("users.html", {"request": request, "users": users})


@router.get("/dialogues/{telegram_id}")
async def dialogues_page(request: Request, telegram_id: int, session: AsyncSession = Depends(get_session)):
    user = (await session.execute(
        text("SELECT first_name, username, telegram_id FROM users WHERE telegram_id = :tid"),
        {"tid": telegram_id},
    )).mappings().first()

    sessions_r = await session.execute(text("""
        SELECT cs.id, cs.started_at, cs.last_message_at, cs.is_active,
               (SELECT COUNT(*) FROM chat_messages WHERE session_id = cs.id) as msg_count
        FROM chat_sessions cs JOIN users u ON cs.user_id = u.id
        WHERE u.telegram_id = :tid ORDER BY cs.started_at DESC
    """), {"tid": telegram_id})
    sessions = [dict(row._mapping) for row in sessions_r.fetchall()]

    messages = []
    if sessions:
        msg_r = await session.execute(text("""
            SELECT cm.role, cm.content, cm.created_at
            FROM chat_messages cm WHERE cm.session_id = CAST(:sid AS uuid)
            ORDER BY cm.created_at
        """), {"sid": str(sessions[0]["id"])})
        messages = [dict(row._mapping) for row in msg_r.fetchall()]

    return templates.TemplateResponse("dialogues.html", {
        "request": request, "user": user, "sessions": sessions,
        "messages": messages, "telegram_id": telegram_id,
    })
