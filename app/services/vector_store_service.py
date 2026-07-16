import hashlib
import random
import httpx
from typing import Any, Dict, List
from openai import AsyncOpenAI
from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

async def get_embedding(text: str) -> List[float]:
    """Generate 1024-dimensional embedding using OpenAI's text-embedding-3-small."""
    s = get_settings()
    api_key = s.OPENAI_API_KEY.strip()
    is_dummy = not api_key or api_key in ("your-openai-api-key", "test", "dummy", "")
    
    if is_dummy:
        # Generate deterministic mock 1024-dim vector
        h = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)
        random.seed(h)
        return [random.uniform(-1, 1) for _ in range(1024)]
        
    try:
        client = AsyncOpenAI(api_key=api_key)
        response = await client.embeddings.create(
            input=text,
            model="text-embedding-3-small",
            dimensions=1024
        )
        return response.data[0].embedding
    except Exception as e:
        log.error("embedding_generation_failed", extra={"error": str(e)})
        # Graceful fallback to pseudo-random vector so app doesn't crash on network error
        h = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)
        random.seed(h)
        return [random.uniform(-1, 1) for _ in range(1024)]

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
        
    # Prepare payloads
    pinecone_vectors = []
    for vec in vectors:
        meta = vec.get("metadata", {})
        meta["connection_id"] = str(connection_id)
        
        text_content = meta.get("text_content", "")
        values = vec.get("values")
        if not values and text_content:
            values = await get_embedding(text_content)
            
        if not values:
            continue
            
        pinecone_vectors.append({
            "id": vec["id"],
            "values": values,
            "metadata": meta
        })
        
    # Pinecone limits upserts to batches, we will batch by 100
    batch_size = 100
    headers = {
        "Api-Key": s.PINECONE_API_KEY,
        "Content-Type": "application/json"
    }
    url = f"{s.PINECONE_HOST}/vectors/upsert"
    
    async with httpx.AsyncClient() as client:
        for i in range(0, len(pinecone_vectors), batch_size):
            batch = pinecone_vectors[i:i + batch_size]
            payload = {
                "vectors": batch,
                "namespace": s.PINECONE_NAMESPACE
            }
            try:
                r = await client.post(url, json=payload, headers=headers, timeout=15.0)
                if r.status_code != 200:
                    log.error("pinecone_upsert_failed", extra={"status": r.status_code, "response": r.text})
            except Exception as e:
                log.error("pinecone_upsert_exception", extra={"error": str(e)})

async def delete_vectors_by_connection(connection_id: str):
    """Delete all vectors associated with a connection_id."""
    s = get_settings()
    if not s.PINECONE_API_KEY or not s.PINECONE_HOST:
        return
        
    headers = {
        "Api-Key": s.PINECONE_API_KEY,
        "Content-Type": "application/json"
    }
    url = f"{s.PINECONE_HOST}/vectors/delete"
    payload = {
        "filter": {
            "connection_id": {"$eq": str(connection_id)}
        },
        "namespace": s.PINECONE_NAMESPACE
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=15.0)
            if r.status_code != 200:
                log.error("pinecone_delete_failed", extra={"status": r.status_code, "response": r.text})
        except Exception as e:
            log.error("pinecone_delete_exception", extra={"error": str(e)})

async def search_relevant_schema(connection_id: str, query: str, top_k: int = 15) -> List[Dict[str, Any]]:
    """Search Pinecone for relevant schema elements based on a query."""
    s = get_settings()
    if not s.PINECONE_API_KEY or not s.PINECONE_HOST:
        log.warning("pinecone_not_configured_returning_empty_search")
        return []
        
    query_vector = await get_embedding(query)
    
    headers = {
        "Api-Key": s.PINECONE_API_KEY,
        "Content-Type": "application/json"
    }
    url = f"{s.PINECONE_HOST}/query"
    payload = {
        "vector": query_vector,
        "topK": top_k,
        "includeMetadata": True,
        "namespace": s.PINECONE_NAMESPACE,
        "filter": {
            "connection_id": {"$eq": str(connection_id)}
        }
    }
    
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=10.0)
            if r.status_code == 200:
                results = r.json()
                return results.get("matches", [])
            else:
                log.error("pinecone_query_failed", extra={"status": r.status_code, "response": r.text})
                return []
        except Exception as e:
            log.error("pinecone_query_exception", extra={"error": str(e)})
            return []
