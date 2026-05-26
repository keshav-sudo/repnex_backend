from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.core.database.session import get_engine
from app.core.redis import get_redis

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict:
    out: dict = {"status": "ok", "db": "ok", "redis": "ok"}
    try:
        engine = get_engine()
        async with engine.connect() as c:
            await c.execute(text("SELECT 1"))
    except Exception as e:
        out["status"] = "degraded"
        out["db"] = e.__class__.__name__
    try:
        await get_redis().ping()
    except Exception as e:
        out["status"] = "degraded"
        out["redis"] = e.__class__.__name__
    return out
