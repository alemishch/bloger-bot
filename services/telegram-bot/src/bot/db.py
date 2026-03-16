"""Thin async DB layer for the bot — user management + onboarding state."""
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
            text("SELECT id, telegram_id, username, first_name, onboarding_status, onboarding_step, profile_data FROM users WHERE telegram_id = :tid"),
            {"tid": telegram_id},
        )).mappings().first()

        if row:
            await session.execute(
                text("UPDATE users SET username = :u, first_name = :fn, last_name = :ln, updated_at = NOW() WHERE telegram_id = :tid"),
                {"u": username, "fn": first_name, "ln": last_name, "tid": telegram_id},
            )
            await session.commit()
            return {
                "id": str(row["id"]),
                "is_new": False,
                "onboarding_status": row["onboarding_status"],
                "onboarding_step": row["onboarding_step"],
                "profile_data": row["profile_data"],
            }

        uid = uuid.uuid4()
        await session.execute(
            text("""INSERT INTO users (id, telegram_id, blogger_id, username, first_name, last_name,
                    is_active, onboarding_status, created_at, updated_at)
                    VALUES (:id, :tid, :bid::bloggerid, :u, :fn, :ln, true, 'not_started'::onboardingstatus, NOW(), NOW())"""),
            {"id": uid, "tid": telegram_id, "bid": blogger_id, "u": username, "fn": first_name, "ln": last_name},
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
            text("UPDATE users SET onboarding_status = :s::onboardingstatus, onboarding_step = :step, updated_at = NOW() WHERE telegram_id = :tid"),
            {"s": status, "step": step, "tid": telegram_id},
        )
        await session.commit()


async def get_user_state(telegram_id: int) -> dict | None:
    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(
            text("SELECT id, onboarding_status, onboarding_step, profile_data FROM users WHERE telegram_id = :tid"),
            {"tid": telegram_id},
        )).mappings().first()
        if not row:
            return None
        return {
            "id": str(row["id"]),
            "onboarding_status": row["onboarding_status"],
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
            text("""INSERT INTO onboarding_responses (id, user_id, blogger_id, step_id, question_text, answer_value, answer_data, created_at)
                    VALUES (:id, :uid, :bid::bloggerid, :sid, :qt, :av, :ad::jsonb, NOW())
                    ON CONFLICT DO NOTHING"""),
            {
                "id": rid, "uid": user_row["id"], "bid": blogger_id,
                "sid": step_id, "qt": question_text, "av": answer_value,
                "ad": None,
            },
        )
        await session.commit()
