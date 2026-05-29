"""
Console search tool for Pinecone templates.
Usage:
  python scripts/search_pinecone.py "your query here"
  python scripts/search_pinecone.py "last 6 months AP invoices" --top-k 5
"""
import sys
from pinecone import Pinecone

PINECONE_API_KEY = "pcsk_8uNjs_JEnKJ8fThatyMGp8pdLM59ukbadsy3ga5awHrUCfBVbrvKmnaXqasWmUfHTAkuX"
PINECONE_HOST    = "https://repnex-0kl271f.svc.aped-4627-b74a.pinecone.io"
PINECONE_INDEX   = "repnex-sql-templates"
NAMESPACE        = "sql-templates"
EMBED_MODEL      = "llama-text-embed-v2"


def search(query: str, top_k: int = 5):
    pc = Pinecone(api_key=PINECONE_API_KEY)
    index = pc.Index(name=PINECONE_INDEX, host=PINECONE_HOST)

    print(f'\n🔍 Query: "{query}"  (top_k={top_k})\n')

    emb = pc.inference.embed(
        model=EMBED_MODEL,
        inputs=[query],
        parameters={"input_type": "query"},
    )
    results = index.query(
        vector=emb.data[0].values,
        top_k=top_k,
        include_metadata=True,
        namespace=NAMESPACE,
    )

    for i, m in enumerate(results.get("matches", []), 1):
        meta = m["metadata"]
        print(f"  #{i}  score={m['score']:.4f}  id={m['id']}")
        print(f"       desc : {meta.get('description', '')}")
        print(f"       module={meta.get('module','')}  category={meta.get('category','')}")
        print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/search_pinecone.py \"your query\" [--top-k N]")
        sys.exit(1)

    query = sys.argv[1]
    top_k = 5
    if "--top-k" in sys.argv:
        idx = sys.argv.index("--top-k")
        top_k = int(sys.argv[idx + 1])

    search(query, top_k)
