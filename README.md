# Repnex Backend

> **Multi-tenant AI-powered analytics backend.**
> Natural language → curated SQL templates → tenant DB → streamed results + LLM insights.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-orange.svg)](https://www.sqlalchemy.org/)
[![License](https://img.shields.io/badge/license-MIT-lightgrey.svg)](#)

## ✨ What it does

Customers connect their own databases (Postgres, MySQL, MSSQL, Oracle), ask analytics questions in plain English, and receive structured rows + AI-generated insights — without ever writing SQL.

```
"Top 10 customers by revenue last month"
        │
        ▼
[ LLM intent ] → template_id + params
        │
        ▼
[ Curated SQL template ] → safe parameter binding
        │
        ▼
[ Customer DB ] → stream rows
        │
        ▼
[ LLM insight ] → "Acme Inc leads with $1.2M; revenue +23%..."
```

## 🚀 Quickstart (5 minutes)

```bash
git clone https://github.com/keshav-sudo/repnex_backend.git
cd repnex_backend
cp .env.example .env
# edit .env: set SECRET_KEY, ENCRYPTION_KEY, OPENAI_API_KEY

docker-compose up -d
docker-compose exec backend alembic -c app/migrations/alembic.ini upgrade head

curl http://localhost:8000/api/v1/health/live
# → {"status":"ok"}
```

Open the interactive API docs: http://localhost:8000/docs

📖 **Full quickstart with end-to-end test**: [`docs/02-quickstart.md`](./docs/02-quickstart.md)

## 📐 Architecture in one picture

```
Client (REST / WebSocket)
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│  FastAPI                                                      │
│   ├─ middleware (request_id, CORS, exceptions)                │
│   ├─ api/v1/    ── endpoints, dependencies (auth/tenancy/RL)  │
│   ├─ services/  ── business logic (org-scoped queries)        │
│   ├─ llm/       ── intent + insight + title (OpenAI)          │
│   ├─ query_     ── template loader + binder + executor        │
│   │   engine/                                                 │
│   ├─ core/      ── config, security, DB, Redis, logging       │
│   └─ utils/     ── pure helpers                               │
└──────────────────────────────────────────────────────────────┘
        │              │                       │
        ▼              ▼                       ▼
   PostgreSQL       Redis             Customer Target DBs
   (metadata)   (cache + RL)     (per-connection async pools)
```

## 🎯 Key features

| Feature | Detail |
|---------|--------|
| **Multi-tenant** | Hard isolation by `org_id` at every query, FK CASCADE, RBAC |
| **Real-time** | WebSocket streaming: status → sql → data → insight → complete |
| **Multi-DB** | Postgres / MySQL / MSSQL / Oracle / CloudSQL |
| **Safe by design** | LLM never writes SQL — picks from curated templates |
| **Encrypted creds** | Fernet AES-256 for customer DB passwords at rest |
| **Token rotation** | JWT access + refresh with replay protection (Redis) |
| **Rate limiting** | Token bucket via Redis Lua (atomic) |
| **Observable** | Structured JSON logs with request_id correlation |

## 📚 Documentation

Everything lives in [`docs/`](./docs/):

| | |
|---|---|
| [Overview](./docs/01-overview.md) | What & why |
| [Quickstart](./docs/02-quickstart.md) | Run it in 5 min |
| [Architecture](./docs/03-architecture.md) | Layers, flows, multi-tenancy |
| [Module: Core](./docs/04-module-core.md) | Config, security, DB, Redis, logging |
| [Module: API](./docs/04-module-api.md) | All endpoints + dependencies |
| [Module: Services](./docs/04-module-services.md) | Business logic (9 services) |
| [Module: LLM](./docs/04-module-llm.md) | OpenAI integration |
| [Module: Query Engine](./docs/04-module-query-engine.md) | Template-based execution |
| [Module: Schemas](./docs/04-module-schemas.md) | Pydantic v2 DTOs |
| [Module: Database](./docs/04-module-database.md) | Schema + migrations |
| [API Reference](./docs/05-api-reference.md) | All 80+ endpoints |
| [Deployment](./docs/06-deployment.md) | Docker, K8s, prod checklist |
| [Development](./docs/07-development.md) | Local dev, tests, conventions |
| [Security](./docs/08-security.md) | Threat model, mitigations |

## 🛠 Tech stack

- **Python 3.11+** · FastAPI 0.115 · Pydantic v2
- **SQLAlchemy 2.0** (async) · asyncpg · Alembic
- **Redis 7** (cache + rate limit) · `redis.asyncio`
- **OpenAI** (gpt-4o / gpt-4o-mini) · tiktoken · tenacity
- **JWT** (python-jose) · **bcrypt** · **Fernet** (cryptography)
- **WebSockets** native FastAPI
- **Docker** multi-stage · **pytest** + respx for tests

## 📂 Repo layout

```
.
├── docs/                       Full documentation (start here)
├── app/
│   ├── main.py                 FastAPI factory + lifespan
│   ├── api/v1/                 HTTP + WebSocket
│   ├── core/                   Config, security, DB, Redis, logging
│   ├── services/               Business logic (org-scoped)
│   ├── llm/                    OpenAI wrapper + prompts
│   ├── query_engine/           Template-based SQL execution
│   ├── schemas/                Pydantic DTOs
│   ├── utils/                  Pure helpers
│   ├── migrations/             Alembic
│   └── tests/                  unit / integration / e2e
├── Dockerfile                  Multi-stage; non-root; tini PID-1
├── docker-compose.yml          postgres + redis + backend
├── requirements.txt
├── pyproject.toml              ruff + mypy + pytest config
└── .env.example                All ~30 env vars documented
```

## 🔒 Security highlights

- **Tenant isolation** at query level (every service filters by `org_id`)
- **Encrypted credentials** with Fernet
- **bcrypt** cost 12 password hashing
- **JWT rotation** with Redis blacklist (replay protection)
- **Rate limiting** per (org, user) via Redis Lua token bucket
- **No free-form SQL from LLM** — only parameter-bound curated templates
- **Sensitive field redaction** in logs

Full threat model: [`docs/08-security.md`](./docs/08-security.md)

## 🧪 Tests

```bash
pytest -q                      # all
pytest -q app/tests/unit       # fast unit tests
pytest --cov=app               # with coverage
```

## 🤝 Contributing

See [`docs/07-development.md`](./docs/07-development.md). TL;DR:

1. Follow the layering: **endpoints → services → helpers/core**
2. Every service query filters by `org_id` (no exceptions)
3. No free-form SQL — use SQLAlchemy `select()` for metadata, templates for target DBs
4. Async everywhere
5. Pydantic at boundaries, ORM internal
6. `ruff` + `mypy` clean; tests pass

## 📜 License

MIT
