# 1 — Overview

## What is Repnex?

**Repnex** is a production-ready, multi-tenant SaaS backend that lets users run analytics on their own databases using natural language. Users connect their PostgreSQL/MySQL/MSSQL/Oracle database, ask questions in plain English ("show me top 10 customers by revenue last month"), and get back charts, tables, and AI-generated insights — without writing SQL.

### High-level flow

```
User: "Top 10 customers by revenue last quarter?"
         │
         ▼
[ LLM intent extractor ] ─→ template_id + parameters
         │
         ▼
[ Curated SQL template ] ─→ parameter binding (safe, no string SQL)
         │
         ▼
[ Target DB executor ] ─→ stream rows over WebSocket
         │
         ▼
[ LLM insight generator ] ─→ "Revenue grew 23%, top customer is..."
```

## Why this design?

| Concern | Solution |
|---------|----------|
| **SQL injection risk from LLM** | LLM never writes SQL. It picks from curated templates. |
| **Multi-tenant data leaks** | `org_id` filter enforced at every service-layer query |
| **Slow LLM responses block UX** | WebSocket streams `status → sql → data → insight` events |
| **Connecting customer DBs** | Per-connection async pools with LRU eviction; encrypted credentials |
| **Token cost explosion** | Rolling context window per session; trimmed by token count |

## Core Features

### ✅ Multi-tenancy
- Hard isolation by `org_id` at every query
- Per-org rate limiting via Redis token bucket
- Per-user RBAC (admin / editor / viewer)

### ✅ AI-powered analytics
- Natural language → curated SQL templates (4 starter templates)
- LLM-generated session titles
- LLM-generated insights from query results

### ✅ Real-time streaming
- WebSocket protocol with discriminated message types
- Stream rows in batches as they arrive from the target DB
- Cancel in-flight queries

### ✅ Multi-DB support
- Postgres / MySQL / MSSQL / Oracle / CloudSQL
- Per-connection async connection pools
- Automatic LRU eviction of idle pools

### ✅ Security-first
- AES-256 (Fernet) encryption for DB credentials at rest
- bcrypt password hashing (cost=12)
- JWT access + refresh token rotation
- Rate limiting (token bucket via Redis Lua script)
- Sensitive field redaction in logs

### ✅ Reports & dashboards
- Save query templates as named "Reports"
- Compose Reports into "Dashboards" with drag-drop layout
- Public/private report sharing per org

## Tech Stack

| Layer | Technology |
|-------|------------|
| Web framework | FastAPI 0.115 |
| ORM | SQLAlchemy 2.0 (async) + asyncpg |
| Validation | Pydantic v2 |
| Database (metadata) | PostgreSQL 16 |
| Cache + rate limit | Redis 7 |
| Migrations | Alembic |
| LLM | OpenAI (gpt-4-turbo / gpt-4o) |
| Auth | JWT (python-jose) + bcrypt |
| Encryption | Fernet (cryptography) |
| Real-time | WebSockets (native FastAPI) |
| Logging | python-json-logger (structured JSON) |
| Container | Docker (multi-stage) |
| Tests | pytest + pytest-asyncio + respx |

## Use Cases

1. **SaaS BI tool** — give your customers natural-language analytics on their own databases
2. **Internal data assistant** — let non-technical teams query the data warehouse
3. **Embedded analytics** — white-label this backend behind your product

## What this project is NOT

- ❌ A chatbot platform — it's specialized for analytics
- ❌ A free-form SQL generator — LLM picks from curated templates only
- ❌ A vector DB / RAG system — query results come from your real DB
- ❌ A frontend — this is backend only (REST + WebSocket)

## File counts

| Metric | Count |
|--------|-------|
| Total source files | ~90 |
| Python modules | ~70 |
| REST endpoints | 80+ |
| Database tables | 13 |
| Pydantic schemas | 50+ |
| LLM prompts | 3 (intent, insight, title) |
| SQL templates | 4 starter (extensible) |

Next → [Quickstart](./02-quickstart.md)
