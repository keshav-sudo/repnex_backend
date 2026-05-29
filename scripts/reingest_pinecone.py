"""
Standalone Pinecone ingest script.
Run from repnex_backend_complete directory:
  python scripts/reingest_pinecone.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

# ── Bootstrap settings so app.core.config works without a running server ──
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "x" * 48)
os.environ.setdefault("FERNET_KEY", "Q5p_T8u-3JkM4G2HzVx6yYbN7cJgKLpQRsTuVwXyZaA=")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("OPENAI_API_KEY", "dummy")

# Real Pinecone credentials
PINECONE_API_KEY = "pcsk_8uNjs_JEnKJ8fThatyMGp8pdLM59ukbadsy3ga5awHrUCfBVbrvKmnaXqasWmUfHTAkuX"
PINECONE_HOST   = "https://repnex-0kl271f.svc.aped-4627-b74a.pinecone.io"
PINECONE_INDEX  = "repnex-sql-templates"
NAMESPACE       = "sql-templates"
EMBED_MODEL     = "llama-text-embed-v2"  # must match Pinecone index native model
BATCH_SIZE      = 50

TEMPLATES_PATH = Path(__file__).parents[1] / ".." / "repnex_sql_templates" / "all_templates_combined.json"


def build_embedding_text(entry: dict) -> str:
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


def run():
    from pinecone import Pinecone

    pc = Pinecone(api_key=PINECONE_API_KEY)
    index = pc.Index(name=PINECONE_INDEX, host=PINECONE_HOST)

    print(f"📂 Loading templates from: {TEMPLATES_PATH.resolve()}")
    raw = json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
    entries = raw if isinstance(raw, list) else raw.get("templates", [])
    print(f"✅ Loaded {len(entries)} templates")

    ids, texts, metadata_list = [], [], []
    for entry in entries:
        tid = entry.get("id")
        if not tid:
            continue
        text = build_embedding_text(entry)
        if not text:
            continue
        ids.append(tid)
        texts.append(text)
        metadata_list.append({
            "description":    entry.get("description", ""),
            "module":         entry.get("module", ""),
            "category":       entry.get("category", ""),
            "sql":            entry.get("sql", ""),
            "params":         json.dumps(entry.get("params", {})),
            "result_columns": json.dumps(entry.get("result_columns", [])),
            "keywords":       json.dumps(entry.get("keywords", [])),
            "embedding_text": entry.get("embedding_text", ""),
        })

    total_upserted = 0
    total_batches  = (len(texts) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(texts), BATCH_SIZE):
        batch_no    = i // BATCH_SIZE + 1
        batch_texts = texts[i : i + BATCH_SIZE]
        batch_ids   = ids[i : i + BATCH_SIZE]
        batch_meta  = metadata_list[i : i + BATCH_SIZE]

        print(f"  🔢 Batch {batch_no}/{total_batches} — embedding {len(batch_texts)} texts …", end=" ", flush=True)
        embeddings = pc.inference.embed(
            model=EMBED_MODEL,
            inputs=batch_texts,
            parameters={"input_type": "passage"},
        )
        vectors = [
            {"id": batch_ids[j], "values": emb.values, "metadata": batch_meta[j]}
            for j, emb in enumerate(embeddings.data)
        ]
        index.upsert(vectors=vectors, namespace=NAMESPACE)
        total_upserted += len(vectors)
        print(f"✅ upserted {len(vectors)}")

    stats = index.describe_index_stats()
    print()
    print(f"🎉 Done! Total upserted: {total_upserted}")
    print(f"📊 Index stats: {dict(stats.get('namespaces', {}))}")

    # Quick relevance test
    print()
    print("🔍 Quick relevance test:")
    test_queries = [
        "last 6 months AP invoices",
        "AP ageing report",
        "withholding tax deducted supplier invoices",
    ]
    for q in test_queries:
        emb = pc.inference.embed(model=EMBED_MODEL, inputs=[q], parameters={"input_type": "query"})
        res = index.query(vector=emb.data[0].values, top_k=3, include_metadata=True, namespace=NAMESPACE)
        print(f"  Query: \"{q}\"")
        for m in res.get("matches", []):
            print(f"    {m['score']:.4f}  {m['id']}")
        print()


if __name__ == "__main__":
    run()
