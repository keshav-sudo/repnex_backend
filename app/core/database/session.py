from __future__ import annotations

from collections.abc import AsyncIterator
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.database.mongo import get_db as get_mongo_db


async def get_db() -> AsyncIterator[AsyncIOMotorDatabase]:
    """Yield the async MongoDB database instance for dependency injection."""
    yield get_mongo_db()
