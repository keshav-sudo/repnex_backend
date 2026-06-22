"""Pinecone vector-store client for SQL template retrieval (RAG)."""

from __future__ import annotations

import json
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from pinecone import Pinecone

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

            keywords_raw = meta.get("keywords", "[]")
            if isinstance(keywords_raw, str):
                try:
                    keywords_list = json.loads(keywords_raw)
                except json.JSONDecodeError:
                    keywords_list = []
            else:
                keywords_list = keywords_raw if isinstance(keywords_raw, list) else []

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
                    "keywords": keywords_list,
                }
            )
        return templates

    def search_with_rerank(
        self,
        query_text: str,
        top_k: int = 5,
        module_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Two-stage retrieval: broad vector search → keyword/semantic reranking.

        Stage 1: Fetch top_k * 4 candidates from Pinecone (NO module filter
                 — filtering was silently returning 0 results when module names
                 didn't match Pinecone's vocabulary)
        Stage 2: Re-rank using keyword overlap + description similarity,
                 with module_filter used as a soft boost signal
        """
        # Stage 1: Broad retrieval — always unfiltered for maximum recall
        candidates = self.search_templates(
            query_text, top_k=top_k * 4, module_filter=None
        )

        if not candidates:
            return []

        # Stage 2: Keyword-boosted reranking
        query_lower = query_text.lower()
        query_words = set(query_lower.split())

        scored = []
        for c in candidates:
            base_score = c.get("score", 0.0)

            # Keyword overlap boost
            keywords = c.get("keywords", [])
            if isinstance(keywords, str):
                try:
                    keywords = json.loads(keywords)
                except Exception:
                    keywords = []

            keyword_hits = sum(
                1 for kw in keywords
                if kw.lower() in query_lower or any(w in kw.lower() for w in query_words)
            )
            keyword_boost = min(keyword_hits * 0.03, 0.12)  # max 12% boost

            # Description word overlap boost
            desc_words = set(c.get("description", "").lower().split())
            desc_overlap = len(query_words & desc_words)
            desc_boost = min(desc_overlap * 0.025, 0.10)  # max 10% boost

            # Module/category match boost from query keywords
            module = c.get("module", "").lower()
            category = c.get("category", "").lower()
            module_boost = 0.06 if module and module in query_lower else 0
            category_boost = 0.04 if category and category in query_lower else 0

            # Detected module soft boost — reward candidates matching the
            # heuristically-detected module from the user's query
            detected_boost = 0.0
            if module_filter and module:
                if module == module_filter.lower():
                    detected_boost = 0.08  # strong signal
                elif module_filter.lower() in module or module in module_filter.lower():
                    detected_boost = 0.04  # partial match

            final_score = base_score + keyword_boost + desc_boost + module_boost + category_boost + detected_boost
            scored.append((final_score, c))

        # Sort by final score and return top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, c in scored[:top_k]:
            c["score"] = score  # Update with reranked score
            results.append(c)

        return results

    def get_template_by_id(self, template_id: str) -> dict[str, Any] | None:
        """Fetch a specific template by ID from the index namespace."""
        try:
            res = self._index.fetch(ids=[template_id], namespace=self._namespace)
            vectors = res.get("vectors", {})
            if template_id not in vectors:
                return None
            vector_data = vectors[template_id]
            meta = vector_data.get("metadata", {})
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
            return {
                "id": template_id,
                "description": meta.get("description", ""),
                "module": meta.get("module", ""),
                "category": meta.get("category", ""),
                "sql": meta.get("sql", ""),
                "params": params,
                "result_columns": result_columns,
            }
        except Exception as e:
            log.warning("pinecone_fetch_failed", extra={"template_id": template_id, "err": str(e)})
            return None

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
