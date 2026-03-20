from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from user_service.config import settings

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
        _session_factory = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False,
        )
    return _session_factory


async def get_session():
    factory = get_session_factory()
    async with factory() as session:
        yield session
