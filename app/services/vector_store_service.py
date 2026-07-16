import hashlib
import random
import httpx
from typing import Any, Dict, List
from openai import AsyncOpenAI
from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


async def get_embedding(text: str) -> List[float]:
    """Generate embedding using the configured provider (DeepSeek → OpenAI → mock)."""
    res = await get_embeddings([text])
    return res[0] if res else [0.0] * 1024


async def get_embeddings(texts: List[str]) -> List[List[float]]:
    """
    Generate embeddings in batch.

    Priority:
      1. DeepSeek  (uses DEEPSEEK_API_KEY — same key you already pay for, zero extra cost)
      2. OpenAI    (uses OPENAI_API_KEY — optional fallback)
      3. Local hash mock (no API needed — search quality approximate but system works)
    """
    if not texts:
        return []

    s = get_settings()

    # ── 1. DeepSeek Embeddings (primary — no extra cost) ─────────────────
    ds_key = (s.DEEPSEEK_API_KEY or "").strip()
    ds_base = (s.DEEPSEEK_BASE_URL or "https://api.deepseek.com/v1").rstrip("/")
    is_valid_ds = bool(ds_key) and ds_key not in ("", "your-deepseek-api-key")

    if is_valid_ds:
        try:
            client = AsyncOpenAI(api_key=ds_key, base_url=ds_base)
            response = await client.embeddings.create(
                input=texts,
                model="deepseek-embedding",   # 1024-dim, OpenAI-compatible endpoint
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            log.warning(
                "deepseek_embedding_failed_falling_back",
                extra={"error": str(e), "count": len(texts)},
            )

    # ── 2. OpenAI Embeddings (optional fallback) ──────────────────────────
    oa_key = (s.OPENAI_API_KEY or "").strip()
    is_valid_oa = bool(oa_key) and oa_key not in ("", "your-openai-api-key", "test", "dummy")

    if is_valid_oa:
        try:
            client = AsyncOpenAI(api_key=oa_key)
            response = await client.embeddings.create(
                input=texts,
                model="text-embedding-3-small",
                dimensions=1024,
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            log.warning(
                "openai_embedding_failed_falling_back",
                extra={"error": str(e), "count": len(texts)},
            )

    # ── 3. Local deterministic hash mock (dev/demo fallback) ─────────────
    # Vectors have no real semantic meaning, but the system will still
    # function end-to-end without crashing.
    log.warning("using_local_hash_mock_embeddings", extra={"count": len(texts)})
    results = []
    for text in texts:
        h = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)
        random.seed(h)
        results.append([random.uniform(-1, 1) for _ in range(1024)])
    return results


async def upsert_vectors(connection_id: str, vectors: List[Dict[str, Any]]):
    """
    Upsert list of vector dicts to Pinecone index.
    Each vector dict in 'vectors' should have:
      - id: str
      - values: List[float] (optional, if omitted we embed metadata.text_content)
      - metadata: Dict[str, Any]
    """
    s = get_settings()
    if not s.PINECONE_API_KEY or not s.PINECONE_HOST:
        log.warning("pinecone_not_configured_skipping_upsert")
        return

    # 1. Collect all texts that need embeddings
    texts_to_embed: List[str] = []
    vector_indices_to_embed: List[int] = []

    for idx, vec in enumerate(vectors):
        values = vec.get("values")
        meta = vec.get("metadata", {})
        text_content = meta.get("text_content", "")
        if not values and text_content:
            texts_to_embed.append(text_content)
            vector_indices_to_embed.append(idx)

    # 2. Batch generate embeddings (100 per request to stay within limits)
    embedded_values: Dict[int, List[float]] = {}
    batch_embed_size = 100
    for i in range(0, len(texts_to_embed), batch_embed_size):
        chunk = texts_to_embed[i : i + batch_embed_size]
        embeddings = await get_embeddings(chunk)
        for offset, emb in enumerate(embeddings):
            original_idx = vector_indices_to_embed[i + offset]
            embedded_values[original_idx] = emb

    # 3. Build the final Pinecone vectors payload
    pinecone_vectors = []
    for idx, vec in enumerate(vectors):
        meta = vec.get("metadata", {})
        meta["connection_id"] = str(connection_id)

        values = vec.get("values") or embedded_values.get(idx)
        if not values:
            continue

        pinecone_vectors.append({"id": vec["id"], "values": values, "metadata": meta})

    # 4. Upsert to Pinecone in batches of 100
    batch_size = 100
    headers = {"Api-Key": s.PINECONE_API_KEY, "Content-Type": "application/json"}
    url = f"{s.PINECONE_HOST}/vectors/upsert"

    async with httpx.AsyncClient() as client:
        for i in range(0, len(pinecone_vectors), batch_size):
            batch = pinecone_vectors[i : i + batch_size]
            payload = {"vectors": batch, "namespace": s.PINECONE_NAMESPACE}
            try:
                r = await client.post(url, json=payload, headers=headers, timeout=15.0)
                if r.status_code != 200:
                    log.error(
                        "pinecone_upsert_failed",
                        extra={"status": r.status_code, "response": r.text},
                    )
            except Exception as e:
                log.error("pinecone_upsert_exception", extra={"error": str(e)})


async def delete_vectors_by_connection(connection_id: str):
    """Delete all vectors associated with a connection_id."""
    s = get_settings()
    if not s.PINECONE_API_KEY or not s.PINECONE_HOST:
        return

    headers = {"Api-Key": s.PINECONE_API_KEY, "Content-Type": "application/json"}
    url = f"{s.PINECONE_HOST}/vectors/delete"
    payload = {
        "filter": {"connection_id": {"$eq": str(connection_id)}},
        "namespace": s.PINECONE_NAMESPACE,
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=15.0)
            if r.status_code != 200:
                log.error(
                    "pinecone_delete_failed",
                    extra={"status": r.status_code, "response": r.text},
                )
        except Exception as e:
            log.error("pinecone_delete_exception", extra={"error": str(e)})


async def search_relevant_schema(
    connection_id: str, query: str, top_k: int = 15
) -> List[Dict[str, Any]]:
    """Search Pinecone for relevant schema elements based on a query."""
    s = get_settings()
    if not s.PINECONE_API_KEY or not s.PINECONE_HOST:
        log.warning("pinecone_not_configured_returning_empty_search")
        return []

    query_vector = await get_embedding(query)

    headers = {"Api-Key": s.PINECONE_API_KEY, "Content-Type": "application/json"}
    url = f"{s.PINECONE_HOST}/query"
    payload = {
        "vector": query_vector,
        "topK": top_k,
        "includeMetadata": True,
        "namespace": s.PINECONE_NAMESPACE,
        "filter": {"connection_id": {"$eq": str(connection_id)}},
    }

    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=10.0)
            if r.status_code == 200:
                return r.json().get("matches", [])
            log.error(
                "pinecone_query_failed",
                extra={"status": r.status_code, "response": r.text},
            )
            return []
        except Exception as e:
            log.error("pinecone_query_exception", extra={"error": str(e)})
            return []
