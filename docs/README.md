# Repnex Backend — Documentation

Welcome to the Repnex Backend documentation. This directory contains a complete guide for understanding, running, and extending the multi-tenant AI analytics platform.

## 📚 Index

| # | Document | Description |
|---|----------|-------------|
| 1 | [Overview](./01-overview.md) | What is Repnex? Features, use cases, tech stack |
| 2 | [Quickstart](./02-quickstart.md) | Get running locally in 5 minutes |
| 3 | [Architecture](./03-architecture.md) | System layers, data flow, multi-tenancy model |
| 4 | [Modules — Core](./04-module-core.md) | Config, security, database, Redis, logging |
| 5 | [Modules — API](./04-module-api.md) | REST + WebSocket endpoints, dependencies |
| 6 | [Modules — Services](./04-module-services.md) | Business logic layer (9 services) |
| 7 | [Modules — LLM](./04-module-llm.md) | OpenAI integration: intent, insight, title |
| 8 | [Modules — Query Engine](./04-module-query-engine.md) | Template-based SQL execution |
| 9 | [Modules — Schemas](./04-module-schemas.md) | Pydantic v2 request/response DTOs |
| 10 | [Modules — Database](./04-module-database.md) | SQLAlchemy models + Alembic migrations |
| 11 | [API Reference](./05-api-reference.md) | All 80+ endpoints with examples |
| 12 | [Deployment](./06-deployment.md) | Docker, env vars, production setup |
| 13 | [Development](./07-development.md) | Local dev, tests, code style |
| 14 | [Security](./08-security.md) | Multi-tenancy, encryption, auth |

## 🎯 Quick Links

- **New to project?** → [Overview](./01-overview.md) → [Quickstart](./02-quickstart.md)
- **Building a feature?** → [Architecture](./03-architecture.md) → [Module docs](./04-module-core.md)
- **Deploying?** → [Deployment](./06-deployment.md)
- **Reading code?** → Each module doc maps file-by-file

## 🗂️ Project Layout

```
repnex_backend/
├── app/                    # Application code
│   ├── main.py             # FastAPI factory + lifespan
│   ├── api/v1/             # HTTP + WebSocket surface
│   ├── core/               # Cross-cutting infrastructure
│   ├── services/           # Business logic
│   ├── llm/                # OpenAI integration
│   ├── query_engine/       # SQL template execution
│   ├── schemas/            # Pydantic DTOs
│   ├── utils/              # Pure helpers
│   ├── migrations/         # Alembic versions
│   └── tests/              # unit / integration / e2e
├── docs/                   # ← You are here
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── pyproject.toml
└── .env.example
```
