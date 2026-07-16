from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies.rate_limit import rate_limit
from app.api.dependencies.tenancy import bind_tenant_context
from app.core.database.session import get_db
from app.core.security.auth import CurrentUser
from app.schemas.connection import (
    AccessGrantRead,
    AccessGrantRequest,
    ConnectionCreate,
    ConnectionRead,
    ConnectionUpdate,
    ListDatabasesRequest,
    ListDatabasesResponse,
    TestConnectionResponse,
)
from app.services import connection_service

router = APIRouter(prefix="/connections", tags=["connections"])


@router.get("", response_model=list[ConnectionRead])
async def list_(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _rl: None = Depends(rate_limit("api")),
) -> list[ConnectionRead]:
    return await connection_service.list_connections(db, current)


@router.post("", response_model=ConnectionRead, status_code=status.HTTP_201_CREATED)
async def create(
    data: ConnectionCreate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _rl: None = Depends(rate_limit("api")),
) -> ConnectionRead:
    return await connection_service.create_connection(db, current, data)


# ── Static sub-routes MUST come before /{conn_id} dynamic routes ─────────────

@router.post("/test", response_model=TestConnectionResponse)
async def test_raw(
    data: ConnectionCreate,
    current: CurrentUser = Depends(bind_tenant_context),
    _rl: None = Depends(rate_limit("api")),
) -> TestConnectionResponse:
    """Test credentials without saving — used by the 'Test Connection' button."""
    return await connection_service.test_raw_connection(current, data)


@router.post("/list-databases", response_model=ListDatabasesResponse)
async def list_databases(
    data: ListDatabasesRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    _rl: None = Depends(rate_limit("api")),
) -> ListDatabasesResponse:
    """
    Connect to the server with provided credentials and return available databases.
    Used to populate the database dropdown before the user picks one.
    """
    try:
        return await connection_service.list_databases(current, data)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/gateway-agents", response_model=list[str])
async def list_gateway_agents(
    current: CurrentUser = Depends(bind_tenant_context),
) -> list[str]:
    """Return names of gateway agents currently connected for this org."""
    from app.services.gateway_manager import get_gateway_manager
    try:
        mgr = get_gateway_manager()
        return mgr.list_active_agents(current.org_id)
    except Exception:
        return []


# ── Dynamic /{conn_id} routes ─────────────────────────────────────────────────

@router.get("/{conn_id}", response_model=ConnectionRead)
async def get(
    conn_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ConnectionRead:
    conn = await connection_service.get_connection(db, current, conn_id)
    return ConnectionRead.model_validate(conn)


@router.patch("/{conn_id}", response_model=ConnectionRead)
async def update(
    conn_id: uuid.UUID,
    data: ConnectionUpdate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ConnectionRead:
    return await connection_service.update_connection(db, current, conn_id, data)


@router.delete("/{conn_id}", status_code=status.HTTP_200_OK)
async def delete(
    conn_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict:
    await connection_service.delete_connection(db, current, conn_id)
    return {"ok": True}


@router.post("/{conn_id}/test", response_model=TestConnectionResponse)
async def test(
    conn_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _rl: None = Depends(rate_limit("api")),
) -> TestConnectionResponse:
    return await connection_service.test_connection(db, current, conn_id)


@router.post("/{conn_id}/sync-schema", response_model=ConnectionRead)
async def sync_schema(
    conn_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _rl: None = Depends(rate_limit("api")),
) -> ConnectionRead:
    try:
        return await connection_service.sync_schema(db, current, conn_id)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/{conn_id}/generate-adapters")
async def generate_adapters_endpoint(
    conn_id: uuid.UUID,
    background_tasks: Any,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _rl: None = Depends(rate_limit("api")),
) -> dict:
    """
    Immediately returns { status: 'started', job_id } and runs the heavy
    LLM + Pinecone work in a FastAPI background task.
    Poll GET /{conn_id}/adapter-status to check progress.
    """
    from fastapi import BackgroundTasks
    from app.services.adapter_generator_service import generate_and_index_adapters
    from datetime import datetime, UTC

    job_id = str(uuid.uuid4())
    conn_str = str(conn_id)

    # Store initial job record
    await db["adapter_jobs"].update_one(
        {"connection_id": conn_str},
        {"$set": {
            "connection_id": conn_str,
            "job_id": job_id,
            "status": "running",
            "progress": "Starting AI schema mapping...",
            "started_at": datetime.now(UTC),
            "finished_at": None,
            "result": None,
            "error": None,
        }},
        upsert=True,
    )

    async def _run():
        try:
            res = await generate_and_index_adapters(db, current, conn_id)
            await db["adapter_jobs"].update_one(
                {"connection_id": conn_str},
                {"$set": {
                    "status": "done",
                    "progress": f"Completed — {res.get('concepts_count', 0)} concepts, {res.get('vectors_indexed', 0)} vectors indexed.",
                    "finished_at": datetime.now(UTC),
                    "result": res,
                    "error": None,
                }},
            )
        except Exception as exc:
            await db["adapter_jobs"].update_one(
                {"connection_id": conn_str},
                {"$set": {
                    "status": "failed",
                    "progress": str(exc),
                    "finished_at": datetime.now(UTC),
                    "error": str(exc),
                }},
            )

    # FastAPI background task — request returns immediately
    bt = BackgroundTasks()
    bt.add_task(_run)
    await bt()   # schedule without blocking

    return {"status": "started", "job_id": job_id}


@router.get("/{conn_id}/adapter-status")
async def adapter_status_endpoint(
    conn_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict:
    """Poll this endpoint to check the import job status."""
    doc = await db["adapter_jobs"].find_one({"connection_id": str(conn_id)})
    if not doc:
        return {"status": "idle", "progress": "No import job found.", "result": None}
    return {
        "status": doc.get("status", "idle"),
        "progress": doc.get("progress", ""),
        "result": doc.get("result"),
        "error": doc.get("error"),
    }


@router.post("/{conn_id}/access", response_model=AccessGrantRead, status_code=status.HTTP_201_CREATED)
async def grant_access(
    conn_id: uuid.UUID,
    data: AccessGrantRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> AccessGrantRead:
    return await connection_service.grant_access(db, current, conn_id, data)


@router.delete("/access/{grant_id}", status_code=status.HTTP_200_OK)
async def revoke_access(
    grant_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict:
    await connection_service.revoke_access(db, current, grant_id)
    return {"ok": True}


@router.get("/{conn_id}/tables", response_model=list[dict[str, Any]])
async def get_tables(
    conn_id: uuid.UUID,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> list[dict[str, Any]]:
    conn = await connection_service.get_connection(db, current, conn_id)
    if not conn.schema_info:
        return []
    tables = conn.schema_info.get("tables", [])
    return [
        {
            "name": t.get("name", ""),
            "columns_count": len(t.get("columns", []))
        }
        for t in tables
    ]


@router.get("/{conn_id}/tables/{table_name}", response_model=list[dict[str, str]])
async def get_table_columns(
    conn_id: uuid.UUID,
    table_name: str,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> list[dict[str, str]]:
    conn = await connection_service.get_connection(db, current, conn_id)
    if not conn.schema_info:
        raise HTTPException(status_code=404, detail="Schema not synced yet")

    tables = conn.schema_info.get("tables", [])
    for t in tables:
        if t.get("name") == table_name:
            return t.get("columns", [])

    raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")
