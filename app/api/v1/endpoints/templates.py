"""Template management endpoints: ingestion, search, status."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1.dependencies.tenancy import bind_tenant_context
from app.core.pinecone_client import get_pinecone_store
from app.core.security.auth import CurrentUser
from app.services.ingest_templates import ingest_templates
from pydantic import BaseModel, Field


router = APIRouter(prefix="/templates", tags=["templates"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)


class IngestResponse(BaseModel):
    templates_processed: int
    vectors_upserted: int
    index_stats: dict


@router.get("/status")
async def template_status(
    _: CurrentUser = Depends(bind_tenant_context),
) -> dict:
    """Get Pinecone index stats."""
    try:
        store = get_pinecone_store()
        return {"status": "connected", **store.get_stats()}
    except RuntimeError:
        return {"status": "not_initialized", "total_vector_count": 0}


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    _: CurrentUser = Depends(bind_tenant_context),
) -> IngestResponse:
    """Push all templates from JSON into Pinecone."""
    result = await ingest_templates()
    return IngestResponse(**result)


@router.post("/search")
async def search_templates(
    data: SearchRequest,
    _: CurrentUser = Depends(bind_tenant_context),
) -> list[dict]:
    """Search templates by natural language."""
    store = get_pinecone_store()
    return store.search_templates(data.query, top_k=data.top_k)
