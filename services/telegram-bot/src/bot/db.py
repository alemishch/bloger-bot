"""Async DB layer for the bot — user management + onboarding state."""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
from bot.config import settings
import uuid

_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url, echo=False, pool_size=5, max_overflow=5,
            connect_args={"prepared_statement_cache_size": 0},
        )
    return _engine


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)
    return _session_factory


async def upsert_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    blogger_id: str = "yuri",
) -> dict:
    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(
            text("""SELECT id, telegram_id, username, first_name,
                           onboarding_status::text, onboarding_step, profile_data
                    FROM users WHERE telegram_id = :tid"""),
            {"tid": telegram_id},
        )).mappings().first()

        if row:
            await session.execute(
                text("""UPDATE users SET username = :u, first_name = :fn,
                        last_name = :ln, updated_at = NOW()
                        WHERE telegram_id = :tid"""),
                {"u": username, "fn": first_name, "ln": last_name, "tid": telegram_id},
            )
            await session.commit()
            return {
                "id": str(row["id"]),
                "is_new": False,
                "onboarding_status": str(row["onboarding_status"]),
                "onboarding_step": row["onboarding_step"],
                "profile_data": row["profile_data"],
            }

        uid = uuid.uuid4()
        await session.execute(
            text("""INSERT INTO users
                    (id, telegram_id, blogger_id, username, first_name, last_name,
                     is_active, onboarding_status, created_at, updated_at)
                    VALUES (:id, :tid, CAST(:bid AS bloggerid), :u, :fn, :ln,
                            true, CAST('not_started' AS onboardingstatus), NOW(), NOW())"""),
            {"id": uid, "tid": telegram_id, "bid": blogger_id,
             "u": username, "fn": first_name, "ln": last_name},
        )
        await session.commit()
        return {
            "id": str(uid),
            "is_new": True,
            "onboarding_status": "not_started",
            "onboarding_step": None,
            "profile_data": None,
        }


async def update_onboarding_state(telegram_id: int, status: str, step: str | None = None):
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            text("""UPDATE users
                    SET onboarding_status = CAST(:s AS onboardingstatus),
                        onboarding_step = :step, updated_at = NOW()
                    WHERE telegram_id = :tid"""),
            {"s": status, "step": step, "tid": telegram_id},
        )
        await session.commit()


async def get_user_state(telegram_id: int) -> dict | None:
    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(
            text("""SELECT id, onboarding_status::text, onboarding_step, profile_data
                    FROM users WHERE telegram_id = :tid"""),
            {"tid": telegram_id},
        )).mappings().first()
        if not row:
            return None
        return {
            "id": str(row["id"]),
            "onboarding_status": str(row["onboarding_status"]),
            "onboarding_step": row["onboarding_step"],
            "profile_data": row["profile_data"],
        }


async def save_onboarding_response(
    telegram_id: int,
    blogger_id: str,
    step_id: str,
    question_text: str,
    answer_value: str,
    answer_data: dict | None = None,
):
    factory = get_session_factory()
    async with factory() as session:
        user_row = (await session.execute(
            text("SELECT id FROM users WHERE telegram_id = :tid"),
            {"tid": telegram_id},
        )).mappings().first()
        if not user_row:
            return

        rid = uuid.uuid4()
        await session.execute(
            text("""INSERT INTO onboarding_responses
                    (id, user_id, blogger_id, step_id, question_text, answer_value, created_at)
                    VALUES (:id, :uid, CAST(:bid AS bloggerid), :sid, :qt, :av, NOW())"""),
            {
                "id": rid, "uid": user_row["id"], "bid": blogger_id,
                "sid": step_id, "qt": question_text, "av": answer_value,
            },
        )
        await session.commit()


async def save_chat_message(
    user_id: str,
    session_id: str,
    role: str,
    content: str,
    token_count: int | None = None,
):
    """Save a message for future Mini App / analytics use."""
    factory = get_session_factory()
    async with factory() as session:
        mid = uuid.uuid4()
        await session.execute(
            text("""INSERT INTO chat_messages (id, session_id, role, content, token_count, created_at)
                    VALUES (:id, :sid, :role, :content, :tc, NOW())"""),
            {"id": mid, "sid": session_id, "role": role, "content": content, "tc": token_count},
        )
        await session.commit()


async def get_or_create_session(telegram_id: int, blogger_id: str) -> str:
    """Get active session or create new one. Returns session_id as string."""
    factory = get_session_factory()
    async with factory() as session:
        user_row = (await session.execute(
            text("SELECT id FROM users WHERE telegram_id = :tid"),
            {"tid": telegram_id},
        )).mappings().first()
        if not user_row:
            return ""

        row = (await session.execute(
            text("""SELECT id, last_message_at FROM chat_sessions
                    WHERE user_id = :uid AND is_active = true
                    ORDER BY last_message_at DESC LIMIT 1"""),
            {"uid": user_row["id"]},
        )).mappings().first()

        if row:
            from datetime import datetime, timedelta
            if datetime.utcnow() - row["last_message_at"] < timedelta(hours=2):
                await session.execute(
                    text("UPDATE chat_sessions SET last_message_at = NOW() WHERE id = :sid"),
                    {"sid": row["id"]},
                )
                await session.commit()
                return str(row["id"])
            else:
                await session.execute(
                    text("UPDATE chat_sessions SET is_active = false, closed_at = NOW() WHERE id = :sid"),
                    {"sid": row["id"]},
                )

        sid = uuid.uuid4()
        await session.execute(
            text("""INSERT INTO chat_sessions (id, user_id, blogger_id, is_active, started_at, last_message_at)
                    VALUES (:id, :uid, CAST(:bid AS bloggerid), true, NOW(), NOW())"""),
            {"id": sid, "uid": user_row["id"], "bid": blogger_id},
        )
        await session.commit()
        return str(sid)


async def get_onboarding_responses(telegram_id: int) -> list[dict]:
    """Get all onboarding responses for a user (for API / Mini App)."""
    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(
            text("""SELECT r.step_id, r.question_text, r.answer_value, r.created_at
                    FROM onboarding_responses r
                    JOIN users u ON r.user_id = u.id
                    WHERE u.telegram_id = :tid
                    ORDER BY r.created_at"""),
            {"tid": telegram_id},
        )).mappings().all()
        return [dict(r) for r in rows]


async def clear_onboarding_responses(telegram_id: int):
    """Delete all onboarding responses for a user (for /reset)."""
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            text("""DELETE FROM onboarding_responses
                    WHERE user_id = (SELECT id FROM users WHERE telegram_id = :tid)"""),
            {"tid": telegram_id},
        )
        await session.commit()


async def get_session_history(telegram_id: int, max_messages: int = 20) -> list[dict]:
    """Get messages from last 2 sessions (hot memory per §14.2)."""
    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(
            text("""
                SELECT cm.role, cm.content
                FROM chat_messages cm
                JOIN chat_sessions cs ON cm.session_id = cs.id
                JOIN users u ON cs.user_id = u.id
                WHERE u.telegram_id = :tid
                ORDER BY cm.created_at DESC
                LIMIT :lim
            """),
            {"tid": telegram_id, "lim": max_messages},
        )).mappings().all()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def get_long_term_profile(telegram_id: int) -> dict | None:
    """Get user's cold profile JSON (per §14.4)."""
    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(
            text("SELECT long_term_profile FROM users WHERE telegram_id = :tid"),
            {"tid": telegram_id},
        )).mappings().first()
        return row["long_term_profile"] if row and row["long_term_profile"] else None


async def update_long_term_profile(telegram_id: int, profile: dict):
    """Update user's cold profile JSON."""
    import json as _json
    profile_str = _json.dumps(profile, ensure_ascii=False)
    if len(profile_str) > 4000:
        profile_str = profile_str[:4000]
        profile = _json.loads(profile_str[:profile_str.rfind('"')] + '"}')
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            text("UPDATE users SET long_term_profile = CAST(:p AS json), updated_at = NOW() WHERE telegram_id = :tid"),
            {"p": _json.dumps(profile, ensure_ascii=False), "tid": telegram_id},
        )
        await session.commit()


async def get_closed_session_for_update(telegram_id: int) -> dict | None:
    """Get most recently closed session that hasn't been summarized yet."""
    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(
            text("""
                SELECT cs.id, cs.closed_at
                FROM chat_sessions cs JOIN users u ON cs.user_id = u.id
                WHERE u.telegram_id = :tid AND cs.is_active = false AND cs.summary IS NULL
                ORDER BY cs.closed_at DESC LIMIT 1
            """),
            {"tid": telegram_id},
        )).mappings().first()
        if not row:
            return None

        msgs = (await session.execute(
            text("SELECT role, content FROM chat_messages WHERE session_id = CAST(:sid AS uuid) ORDER BY created_at"),
            {"sid": str(row["id"])},
        )).mappings().all()

        return {
            "session_id": str(row["id"]),
            "messages": [{"role": m["role"], "content": m["content"]} for m in msgs],
        }


async def mark_session_summarized(session_id: str, summary: str):
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            text("UPDATE chat_sessions SET summary = :s WHERE id = CAST(:sid AS uuid)"),
            {"s": summary[:2000], "sid": session_id},
        )
        await session.commit()
