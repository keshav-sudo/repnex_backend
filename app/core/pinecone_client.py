"""Pinecone vector-store client for SQL template retrieval (RAG)."""
from __future__ import annotations

import json
from typing import Any

from pinecone import Pinecone

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


class PineconeTemplateStore:
    """Thin wrapper around Pinecone for template search."""

    def __init__(self) -> None:
        s = get_settings()
        self._pc = Pinecone(api_key=s.PINECONE_API_KEY)
        self._index = self._pc.Index(
            name=s.PINECONE_INDEX_NAME,
            host=s.PINECONE_HOST,
        )
        self._namespace = "sql-templates"
        log.info("pinecone_connected", extra={"index": s.PINECONE_INDEX_NAME})

    # ── search ───────────────────────────────────────────────────────
    def search_templates(
        self,
        query_text: str,
        top_k: int = 5,
        module_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Embed *query_text* via Pinecone inference and search the index."""

        # Build the query vector using Pinecone's integrated inference
        # IMPORTANT: must match index native model (llama-text-embed-v2)
        embedding = self._pc.inference.embed(
            model="llama-text-embed-v2",
            inputs=[query_text],
            parameters={"input_type": "query"},
        )

        vector = embedding.data[0].values

        # Optional metadata filter
        filter_dict: dict[str, Any] | None = None
        if module_filter:
            filter_dict = {"module": {"$eq": module_filter}}

        results = self._index.query(
            vector=vector,
            top_k=top_k,
            include_metadata=True,
            filter=filter_dict,
            namespace=self._namespace,
        )

        templates: list[dict[str, Any]] = []
        for match in results.get("matches", []):
            meta = match.get("metadata", {})
            # Parse JSON-string fields back into dicts/lists
            params = meta.get("params", "{}")
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except json.JSONDecodeError:
                    params = {}

            result_columns = meta.get("result_columns", "[]")
            if isinstance(result_columns, str):
                try:
                    result_columns = json.loads(result_columns)
                except json.JSONDecodeError:
                    result_columns = []

            templates.append(
                {
                    "id": match["id"],
                    "score": match.get("score", 0.0),
                    "description": meta.get("description", ""),
                    "module": meta.get("module", ""),
                    "category": meta.get("category", ""),
                    "sql": meta.get("sql", ""),
                    "params": params,
                    "result_columns": result_columns,
                }
            )
        return templates

    # ── upsert (for ingestion) ───────────────────────────────────────
    def upsert_batch(
        self,
        vectors: list[dict[str, Any]],
    ) -> int:
        """Upsert a batch of vectors. Returns count upserted."""
        self._index.upsert(vectors=vectors, namespace=self._namespace)
        return len(vectors)

    # ── stats ─────────────────────────────────────────────────────────
    def get_stats(self) -> dict[str, Any]:
        stats = self._index.describe_index_stats()
        return {
            "total_vector_count": stats.get("total_vector_count", 0),
            "dimension": stats.get("dimension", 0),
            "namespaces": stats.get("namespaces", {}),
        }


# ── singleton ─────────────────────────────────────────────────────────

_store: PineconeTemplateStore | None = None


def init_pinecone_store() -> PineconeTemplateStore:
    global _store
    s = get_settings()
    if not s.PINECONE_API_KEY:
        log.warning("pinecone_disabled", extra={"reason": "no api key"})
        return None  # type: ignore[return-value]
    _store = PineconeTemplateStore()
    return _store


def get_pinecone_store() -> PineconeTemplateStore:
    if _store is None:
        raise RuntimeError("Pinecone store not initialized")
    return _store


def get_pinecone_store_optional() -> PineconeTemplateStore | None:
    """Return the store or None (for graceful degradation)."""
    return _store
