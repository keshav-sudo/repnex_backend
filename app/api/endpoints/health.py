from __future__ import annotations

from fastapi import APIRouter

from app.core.database.mongo import get_db as get_mongo_db
from app.core.redis import get_redis

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict:
    out: dict = {"status": "ok", "db": "ok", "redis": "ok"}
    try:
        db = get_mongo_db()
        await db.client.admin.command('ping')
    except Exception as e:
        out["status"] = "degraded"
        out["db"] = e.__class__.__name__
    try:
        r = get_redis()
        if r is not None:
            await r.ping()
        else:
            out["redis"] = "disabled"
    except Exception as e:
        out["status"] = "degraded"
        out["redis"] = e.__class__.__name__
    return out


@router.get("/metrics")
async def metrics() -> dict:
    """Scalability metrics endpoint for monitoring dashboards."""
    from app.services.gateway_manager import get_gateway_manager
    from app.services.websocket_manager import get_ws_manager

    ws_mgr = get_ws_manager()
    gw_mgr = get_gateway_manager()

    return {
        "websocket_connections": ws_mgr.total_connections,
        "gateway_agents": len(gw_mgr._agents),
        "ws_warn_threshold": ws_mgr.WARN_TOTAL_CONNECTIONS,
        "ws_max_per_session": ws_mgr.MAX_CONNECTIONS_PER_SESSION,
    }
