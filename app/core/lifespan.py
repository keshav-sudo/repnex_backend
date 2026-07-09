"""Application lifespan — startup and graceful shutdown sequence."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from app.core.config import get_settings
from app.core.database.mongo import (
    close_mongo,
    ensure_indexes,
    init_mongo,
)
from app.core.database.mongo import (
    get_db as get_mongo_db,
)
from app.core.database.session import get_db
from app.core.database.target_pool import close_target_pool_registry, init_target_pool_registry
from app.core.logging import get_logger, setup_logging
from app.core.redis import close_redis, init_redis
from app.services import report_service
from app.services.gateway_manager import init_gateway_manager
from app.services.websocket_manager import init_ws_manager, shutdown_ws_manager
from fastapi import FastAPI

log = get_logger(__name__)

# ── Optional APScheduler ──────────────────────────────────────────────────────
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    _scheduler: AsyncIOScheduler | None = AsyncIOScheduler()
    _scheduler_available = True
except ImportError:
    _scheduler = None
    _scheduler_available = False


async def _scheduled_refresh_job() -> None:
    """Hourly job: run all reports whose next_refresh_at is due."""
    try:
        async for db in get_db():
            await report_service.run_due_reports(db)
    except Exception as exc:  # noqa: BLE001
        log.error("scheduled_refresh_error", extra={"err": str(exc)})


def _start_scheduler() -> None:
    if not (_scheduler_available and _scheduler):
        log.warning("apscheduler_unavailable", extra={"hint": "pip install apscheduler"})
        return
    _scheduler.add_job(
        _scheduled_refresh_job,
        trigger="interval",
        hours=1,
        id="report_auto_refresh",
        replace_existing=True,
        max_instances=1,
    )
    _scheduler.start()
    log.info("scheduler_started", extra={"job": "report_auto_refresh", "interval": "1h"})


def _stop_scheduler() -> None:
    if _scheduler_available and _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")


def _parse_db_name(database_url: str) -> str:
    """Extract database name from a MongoDB connection string."""
    db_name = "repnex"
    cleaned = database_url
    if "://" in cleaned:
        cleaned = cleaned.split("://", 1)[1]
    if "/" in cleaned:
        parts = cleaned.split("/", 1)
        if len(parts) > 1 and parts[1]:
            candidate = parts[1].split("?")[0]
            if candidate and "=" not in candidate and "&" not in candidate:
                db_name = candidate
    return db_name


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """FastAPI lifespan — manages all service startup and shutdown."""
    setup_logging()
    settings = get_settings()
    log.info("startup", extra={"env": settings.APP_ENV})

    init_mongo(settings.DATABASE_URL, db_name=_parse_db_name(settings.DATABASE_URL))
    await ensure_indexes(get_mongo_db())
    await init_redis()
    init_target_pool_registry()
    init_ws_manager()
    init_gateway_manager()
    _start_scheduler()

    log.info("ready")
    try:
        yield
    finally:
        log.info("shutdown_begin")
        _stop_scheduler()
        try:
            await asyncio.wait_for(
                shutdown_ws_manager(), timeout=settings.GRACEFUL_SHUTDOWN_SECONDS
            )
        except TimeoutError:
            log.warning("ws_shutdown_timeout")
        await close_target_pool_registry()
        await close_redis()
        await close_mongo()
        log.info("shutdown_complete")
