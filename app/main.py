from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.router import api_router, ws_router
from app.core.config import get_settings
from app.core.database.session import dispose_engine, init_engine
from app.core.database.target_pool import (
    close_target_pool_registry,
    init_target_pool_registry,
)
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, request_id_ctx, setup_logging
from app.core.redis import close_redis, init_redis
from app.query_engine.template_loader import init_template_registry
from app.services.websocket_manager import init_ws_manager, shutdown_ws_manager

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    log.info("startup", extra={"app_env": settings.APP_ENV})

    init_engine()
    await init_redis()
    init_template_registry()
    init_target_pool_registry()
    init_ws_manager()

    log.info("ready")
    try:
        yield
    finally:
        log.info("shutdown_begin")
        try:
            await asyncio.wait_for(
                shutdown_ws_manager(), timeout=settings.GRACEFUL_SHUTDOWN_SECONDS
            )
        except asyncio.TimeoutError:
            log.warning("ws_shutdown_timeout")
        await close_target_pool_registry()
        await close_redis()
        await dispose_engine()
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
