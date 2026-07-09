from __future__ import annotations

from collections.abc import AsyncIterator

from app.core.database.mongo import get_db as get_mongo_db
from motor.motor_asyncio import AsyncIOMotorDatabase


async def get_db() -> AsyncIterator[AsyncIOMotorDatabase]:
    """Yield the async MongoDB database instance for dependency injection."""
    yield get_mongo_db()
