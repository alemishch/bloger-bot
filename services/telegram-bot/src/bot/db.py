"""Thin async DB layer for the bot — user upsert on /start."""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, text
from bot.config import settings
import uuid
from datetime import datetime

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


async def upsert_user(telegram_id: int, username: str | None, first_name: str | None, last_name: str | None) -> dict:
    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(
            text("SELECT id, telegram_id, username, first_name, profile_data, created_at FROM users WHERE telegram_id = :tid"),
            {"tid": telegram_id},
        )).mappings().first()

        if row:
            await session.execute(
                text("UPDATE users SET username = :u, first_name = :fn, last_name = :ln, updated_at = NOW() WHERE telegram_id = :tid"),
                {"u": username, "fn": first_name, "ln": last_name, "tid": telegram_id},
            )
            await session.commit()
            return {"id": str(row["id"]), "is_new": False, "profile_data": row["profile_data"]}

        uid = uuid.uuid4()
        await session.execute(
            text("""INSERT INTO users (id, telegram_id, username, first_name, last_name, is_active, created_at, updated_at)
                    VALUES (:id, :tid, :u, :fn, :ln, true, NOW(), NOW())"""),
            {"id": uid, "tid": telegram_id, "u": username, "fn": first_name, "ln": last_name},
        )
        await session.commit()
        return {"id": str(uid), "is_new": True, "profile_data": None}
