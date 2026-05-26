from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin,
    auth,
    connections,
    dashboards,
    health,
    organizations,
    query,
    reports,
    sessions,
    users,
    websocket,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(organizations.router)
api_router.include_router(connections.router)
api_router.include_router(sessions.router)
api_router.include_router(query.router)
api_router.include_router(reports.router)
api_router.include_router(dashboards.router)
api_router.include_router(admin.router)

# WebSocket router has no /v1 prefix in the spec — mounted at root in main.py
ws_router = websocket.router
