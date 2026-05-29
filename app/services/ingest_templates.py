"""Push SQL templates from all_templates_combined.json into Pinecone."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.core.pinecone_client import get_pinecone_store

log = get_logger(__name__)

COMBINED_PATH = Path(__file__).parents[2] / ".." / "repnex_sql_templates" / "all_templates_combined.json"


def _build_embedding_text(entry: dict[str, Any]) -> str:
    """Combine fields into a rich embedding text matching server.js format for consistency."""
    parts = [
        entry.get("embedding_text", ""),
        entry.get("description", ""),
        f"Module: {entry.get('module', '')}",
        f"Category: {entry.get('category', '')}",
    ]
    keywords = entry.get("keywords", [])
    if keywords:
        parts.append("Keywords: " + ", ".join(keywords))
    examples = entry.get("example_questions", [])
    if examples:
        parts.append("Example questions: " + " | ".join(examples))
    return "\n".join(p for p in parts if p).strip()


async def ingest_templates(
    path: Path = COMBINED_PATH,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Read combined templates JSON and upsert into Pinecone."""
    store = get_pinecone_store()

    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw if isinstance(raw, list) else raw.get("templates", [])

    log.info("ingest_start", extra={"total_templates": len(entries)})

    # Build embedding texts
    texts = []
    ids = []
    metadata_list = []

    for entry in entries:
        tid = entry["id"]
        text = _build_embedding_text(entry)
        if not text:
            continue

        ids.append(tid)
        texts.append(text)
        metadata_list.append({
            "description": entry.get("description", ""),
            "module": entry.get("module", ""),
            "category": entry.get("category", ""),
            "sql": entry.get("sql", ""),
            "params": json.dumps(entry.get("params", {})),
            "result_columns": json.dumps(entry.get("result_columns", [])),
            "keywords": json.dumps(entry.get("keywords", [])),
            "embedding_text": entry.get("embedding_text", ""),
        })

    # Generate embeddings via Pinecone inference
    from pinecone import Pinecone
    from app.core.config import get_settings

    s = get_settings()
    pc = Pinecone(api_key=s.PINECONE_API_KEY)

    total_upserted = 0

    for i in range(0, len(texts), batch_size):
        batch_texts = texts[i : i + batch_size]
        batch_ids = ids[i : i + batch_size]
        batch_meta = metadata_list[i : i + batch_size]

        # IMPORTANT: must use index native model (llama-text-embed-v2)
        embeddings = pc.inference.embed(
            model="llama-text-embed-v2",
            inputs=batch_texts,
            parameters={"input_type": "passage"},
        )

        vectors = []
        for j, emb in enumerate(embeddings.data):
            vectors.append({
                "id": batch_ids[j],
                "values": emb.values,
                "metadata": batch_meta[j],
            })

        count = store.upsert_batch(vectors)
        total_upserted += count
        log.info("ingest_batch", extra={"batch": i // batch_size + 1, "count": count})

    stats = store.get_stats()
    result = {
        "templates_processed": len(entries),
        "vectors_upserted": total_upserted,
        "index_stats": stats,
    }
    log.info("ingest_complete", extra=result)
    return result
