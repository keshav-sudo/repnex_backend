"""API Router — registers all HTTP endpoints under /api prefix.

WebSocket router is mounted at root (no /api prefix) in main.py.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.endpoints import (
    admin,
    agent,
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
api_router.include_router(agent.router)
api_router.include_router(users.router)
api_router.include_router(organizations.router)
api_router.include_router(connections.router)
api_router.include_router(sessions.router)
api_router.include_router(query.router)
api_router.include_router(reports.router)
api_router.include_router(dashboards.router)
api_router.include_router(admin.router)

# WebSocket has no /api prefix — mounted directly at root in main.py
ws_router = websocket.router
