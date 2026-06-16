from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    settings = get_settings()
    _engine = create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
        pool_pre_ping=True,
        future=True,
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    log.info("db_engine_initialized", extra={
        "pool_size": settings.DB_POOL_SIZE,
        "pool_recycle": settings.DB_POOL_RECYCLE,
        "pool_pre_ping": True,
    })
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Engine not initialized")
    return _engine


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield an async DB session with explicit commit/rollback handling.

    The session is:
    - Committed on successful completion (no exceptions)
    - Rolled back on any exception
    - Always closed when the context exits
    """
    if _sessionmaker is None:
        raise RuntimeError("Sessionmaker not initialized")
    async with _sessionmaker() as session:
        try:
            yield session
            # Explicit commit on success — don't rely on implicit behavior
            await session.commit()
        except Exception:
            await session.rollback()
            raise

