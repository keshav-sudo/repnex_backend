from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.router import api_router, ws_router
from app.core.config import get_settings
from app.core.database.session import get_db
from app.core.database.mongo import init_mongo, ensure_indexes, get_db as get_mongo_db, close_mongo
from app.core.database.target_pool import (
    close_target_pool_registry,
    init_target_pool_registry,
)
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, request_id_ctx, setup_logging
from app.core.pinecone_client import init_pinecone_store
from app.core.redis import close_redis, init_redis
from app.query_engine.template_loader import init_template_registry
from app.services.websocket_manager import init_ws_manager, shutdown_ws_manager
from app.services.gateway_manager import init_gateway_manager
from app.services import report_service

log = get_logger(__name__)


# ── APScheduler setup ─────────────────────────────────────────────────────────
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    _scheduler = AsyncIOScheduler()
    _scheduler_available = True
except ImportError:
    _scheduler = None  # type: ignore[assignment]
    _scheduler_available = False


async def _scheduled_refresh_job() -> None:
    """Hourly APScheduler job: run all reports whose next_refresh_at is due."""
    try:
        async for db in get_db():
            await report_service.run_due_reports(db)
    except Exception as exc:  # noqa: BLE001
        log.error("scheduled_refresh_job_error", extra={"error": str(exc)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    log.info("startup", extra={"app_env": settings.APP_ENV})

    # Parse database name from connection string
    db_name = "repnex"
    cleaned_url = settings.DATABASE_URL
    if "://" in cleaned_url:
        cleaned_url = cleaned_url.split("://", 1)[1]
    if "/" in cleaned_url:
        parts = cleaned_url.split("/", 1)
        if len(parts) > 1 and parts[1]:
            possible_db = parts[1].split("?")[0]
            if possible_db and "=" not in possible_db and "&" not in possible_db:
                db_name = possible_db

    init_mongo(settings.DATABASE_URL, db_name=db_name)
    await ensure_indexes(get_mongo_db())

    await init_redis()
    init_template_registry()
    init_target_pool_registry()
    init_pinecone_store()
    init_ws_manager()
    init_gateway_manager()

    # ── Start scheduled report refresh ────────────────────────────────────
    if _scheduler_available and _scheduler is not None:
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
    else:
        log.warning("apscheduler_not_available", extra={"hint": "pip install apscheduler"})

    log.info("ready")
    try:
        yield
    finally:
        log.info("shutdown_begin")
        if _scheduler_available and _scheduler is not None and _scheduler.running:
            _scheduler.shutdown(wait=False)
            log.info("scheduler_stopped")
        try:
            await asyncio.wait_for(
                shutdown_ws_manager(), timeout=settings.GRACEFUL_SHUTDOWN_SECONDS
            )
        except asyncio.TimeoutError:
            log.warning("ws_shutdown_timeout")
        await close_target_pool_registry()
        await close_redis()
        await close_mongo()
        log.info("shutdown_complete")


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = rid
            return response
        finally:
            request_id_ctx.reset(token)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Repnex Backend",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIdMiddleware)

    app.include_router(api_router, prefix="/v1")
    app.include_router(ws_router)

    register_exception_handlers(app)
    return app


app = create_app()
