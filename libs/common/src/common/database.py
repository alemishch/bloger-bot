from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from common.config import DatabaseSettings
from typing import AsyncIterator

_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        db = DatabaseSettings()
        _engine = create_async_engine(
            db.async_url,
            echo=False,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            # CRITICAL: disable prepared statement cache to avoid
            # InvalidCachedStatementError after schema changes
            connect_args={"prepared_statement_cache_size": 0},
        )
    return _engine


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        yield session