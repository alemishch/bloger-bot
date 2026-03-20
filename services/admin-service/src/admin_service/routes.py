"""Admin REST API — JSON endpoints for dashboard data, user management, export."""
import csv
import io
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from admin_service.db import get_session

router = APIRouter()


# ── Dashboard stats ─────────────────────────────────────────────────────────

@router.get("/stats/overview")
async def stats_overview(session: AsyncSession = Depends(get_session)):
    """Main dashboard numbers."""
    rows = {}
    for q, key in [
        ("SELECT COUNT(*) FROM users", "total_users"),
        ("SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '24 hours'", "new_users_24h"),
        ("SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '7 days'", "new_users_7d"),
        ("SELECT COUNT(*) FROM users WHERE onboarding_status = 'completed'::onboardingstatus", "onboarding_completed"),
        ("SELECT COUNT(*) FROM users WHERE onboarding_status = 'in_progress'::onboardingstatus", "onboarding_in_progress"),
        ("SELECT COUNT(*) FROM chat_sessions", "total_sessions"),
        ("SELECT COUNT(*) FROM chat_sessions WHERE started_at > NOW() - INTERVAL '24 hours'", "sessions_24h"),
        ("SELECT COUNT(*) FROM chat_messages", "total_messages"),
        ("SELECT COUNT(*) FROM chat_messages WHERE created_at > NOW() - INTERVAL '24 hours'", "messages_24h"),
        ("SELECT COUNT(*) FROM chat_messages WHERE role = 'user'", "user_messages"),
        ("SELECT COUNT(*) FROM chat_messages WHERE role = 'assistant'", "bot_messages"),
    ]:
        r = (await session.execute(text(q))).scalar()
        rows[key] = r or 0

    total = rows["total_users"] or 1
    rows["onboarding_rate"] = round(rows["onboarding_completed"] / total * 100, 1)
    return rows


@router.get("/stats/pipeline")
async def stats_pipeline(session: AsyncSession = Depends(get_session)):
    """Content pipeline stats by status."""
    r = await session.execute(
        text("SELECT status::text, COUNT(*) as cnt FROM content_items GROUP BY status ORDER BY cnt DESC")
    )
    return {row[0]: row[1] for row in r.fetchall()}


@router.get("/stats/onboarding")
async def stats_onboarding(session: AsyncSession = Depends(get_session)):
    """Onboarding analytics: popular symptoms, completion funnel."""
    steps = await session.execute(
        text("""SELECT step_id, COUNT(DISTINCT user_id) as users
                FROM onboarding_responses GROUP BY step_id ORDER BY users DESC""")
    )
    step_counts = {row[0]: row[1] for row in steps.fetchall()}

    symptoms = await session.execute(
        text("""SELECT answer_value, COUNT(*) as cnt
                FROM onboarding_responses WHERE step_id = 'symptoms'
                GROUP BY answer_value ORDER BY cnt DESC LIMIT 20""")
    )
    symptom_data = []
    for row in symptoms.fetchall():
        for v in row[0].split(","):
            symptom_data.append(v.strip())

    from collections import Counter
    symptom_counts = dict(Counter(symptom_data).most_common(10))

    return {"steps_completion": step_counts, "top_symptoms": symptom_counts}


@router.get("/stats/activity")
async def stats_activity(days: int = Query(30, le=90), session: AsyncSession = Depends(get_session)):
    """Daily active users and messages for the last N days."""
    r = await session.execute(text("""
        SELECT d::date as day,
               COALESCE(u.cnt, 0) as active_users,
               COALESCE(m.cnt, 0) as messages
        FROM generate_series(NOW() - MAKE_INTERVAL(days => :days), NOW(), '1 day') d
        LEFT JOIN (
            SELECT DATE(started_at) as day, COUNT(DISTINCT user_id) as cnt
            FROM chat_sessions GROUP BY DATE(started_at)
        ) u ON d::date = u.day
        LEFT JOIN (
            SELECT DATE(created_at) as day, COUNT(*) as cnt
            FROM chat_messages GROUP BY DATE(created_at)
        ) m ON d::date = m.day
        ORDER BY day
    """), {"days": days})
    return [{"day": str(row[0]), "active_users": row[1], "messages": row[2]} for row in r.fetchall()]


# ── User management ─────────────────────────────────────────────────────────

@router.get("/users")
async def list_users(
    limit: int = Query(50, le=500),
    offset: int = Query(0),
    status: str = Query(None),
    session: AsyncSession = Depends(get_session),
):
    where = ""
    params = {"lim": limit, "off": offset}
    if status:
        where = "WHERE onboarding_status = CAST(:status AS onboardingstatus)"
        params["status"] = status

    r = await session.execute(text(f"""
        SELECT id, telegram_id, blogger_id::text, username, first_name, last_name,
               onboarding_status::text, onboarding_step, created_at,
               (SELECT COUNT(*) FROM chat_messages cm
                JOIN chat_sessions cs ON cm.session_id = cs.id
                WHERE cs.user_id = u.id) as message_count
        FROM users u {where}
        ORDER BY created_at DESC LIMIT :lim OFFSET :off
    """), params)

    users = []
    for row in r.mappings().all():
        users.append({**dict(row), "id": str(row["id"]),
                       "created_at": str(row["created_at"]) if row["created_at"] else None})
    return users


@router.get("/users/{telegram_id}/dialogues")
async def user_dialogues(telegram_id: int, session: AsyncSession = Depends(get_session)):
    r = await session.execute(text("""
        SELECT cs.id as session_id, cs.started_at, cs.last_message_at, cs.is_active,
               (SELECT COUNT(*) FROM chat_messages WHERE session_id = cs.id) as msg_count
        FROM chat_sessions cs JOIN users u ON cs.user_id = u.id
        WHERE u.telegram_id = :tid ORDER BY cs.started_at DESC
    """), {"tid": telegram_id})
    return [{"session_id": str(row[0]), "started_at": str(row[1]),
             "last_message_at": str(row[2]), "is_active": row[3], "msg_count": row[4]}
            for row in r.fetchall()]


@router.get("/dialogues/{session_id}")
async def dialogue_messages(session_id: str, session: AsyncSession = Depends(get_session)):
    r = await session.execute(text("""
        SELECT role, content, token_count, created_at
        FROM chat_messages WHERE session_id = CAST(:sid AS uuid) ORDER BY created_at
    """), {"sid": session_id})
    return [{"role": row[0], "content": row[1], "tokens": row[2], "created_at": str(row[3])}
            for row in r.fetchall()]


# ── Content export (CSV for labeling verification) ──────────────────────────

@router.get("/export/content")
async def export_content(
    status: str = Query("ready"),
    format: str = Query("csv"),
    session: AsyncSession = Depends(get_session),
):
    """Export labeled content as CSV for expert verification (per TASK.md §5.3)."""
    r = await session.execute(text("""
        SELECT ci.source_message_id, ci.content_type::text, ci.status::text,
               ci.title, LEFT(ci.text, 500) as text_preview,
               LEFT(ci.transcript_text, 500) as transcript_preview,
               ci.summary, ci.tags::text, ci.themes::text,
               ci.problems_solved::text, ci.tools_mentioned::text,
               ci.target_audience, ci.content_category,
               cs.name as source_name, ci.blogger_id::text,
               ci.created_at
        FROM content_items ci
        JOIN content_sources cs ON ci.source_id = cs.id
        WHERE ci.status = CAST(:status AS jobstatus)
        ORDER BY ci.created_at
    """), {"status": status})

    rows = r.fetchall()
    columns = ["message_id", "type", "status", "title", "text_preview", "transcript_preview",
               "summary", "tags", "themes", "problems_solved", "tools_mentioned",
               "target_audience", "content_category", "source", "blogger", "created_at"]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([str(v) if v is not None else "" for v in row])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=content_export_{status}.csv"},
    )
