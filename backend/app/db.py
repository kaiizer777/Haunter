from typing import AsyncGenerator

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# Pooled engine for application runtime - use NullPool because Neon's PgBouncer handles pooling
engine = create_async_engine(
    settings.async_database_url,
    poolclass=NullPool,
    echo=False,
)

async_session_maker = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Unpooled engine for migrations (to be used by Alembic)
engine_unpooled = create_async_engine(
    settings.async_database_url_unpooled,
    poolclass=NullPool,
    echo=False,
)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()
