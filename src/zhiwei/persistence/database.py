"""Async SQLAlchemy engine and session construction."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_database_engine(url: str) -> AsyncEngine:
    """Create an async engine without reading process or dotenv configuration."""
    if not url.startswith(("postgresql+asyncpg://", "postgresql://")):
        raise ValueError("database URL must use PostgreSQL")
    async_url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(async_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create sessions whose ORM values remain usable after commit."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
