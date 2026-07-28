"""Async engine / session factory construction."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jobtrack.config import Settings

SessionFactory = async_sessionmaker[AsyncSession]


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)
