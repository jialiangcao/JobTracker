from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jobtrack.config import Settings
from jobtrack.db.engine import SessionFactory
from jobtrack.db.models import Base


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="sqlite+aiosqlite://",
        min_delay_ms=0,
        max_delay_ms=0,
        backoff_base_seconds=0.01,
        backoff_cap_seconds=0.05,
        discord_pace_seconds=0.0,
        discord_bot_token="test-token",
        discord_channel_id="123456",
    )


@pytest.fixture
async def session_factory() -> AsyncIterator[SessionFactory]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
