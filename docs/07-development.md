# 13 — Development Guide

## Local setup

### 1. Install Python 3.11+

```bash
python --version    # should be 3.11.x or 3.12.x
```

### 2. Create virtualenv & install deps

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Start Postgres + Redis

Easy: `docker-compose up -d postgres redis` (uses the included `docker-compose.yml` but skips backend).

### 4. Configure `.env`

```bash
cp .env.example .env
# edit values; see docs/06-deployment.md for full var list
```

### 5. Migrate

```bash
alembic -c app/migrations/alembic.ini upgrade head
```

### 6. Run

```bash
uvicorn app.main:app --reload --port 8000
```

Browse to http://localhost:8000/docs.

## Project layout (refresher)

| Folder | Purpose |
|--------|---------|
| `app/main.py` | FastAPI factory + lifespan |
| `app/api/v1/` | HTTP + WebSocket surface |
| `app/services/` | Business logic |
| `app/core/` | Cross-cutting infra |
| `app/llm/` | OpenAI wrapper + prompts |
| `app/query_engine/` | Templates + executor |
| `app/schemas/` | Pydantic DTOs |
| `app/utils/` | Pure helpers |
| `app/migrations/` | Alembic |
| `app/tests/` | unit + integration + e2e |

## Coding rules

### 1. Tenancy first
Every service-layer query **must** filter by `org_id` from `TenantCtx`. Reviews block on this.

### 2. No string SQL
Use SQLAlchemy `select()` for metadata; templates for target DBs. Concatenation is forbidden.

### 3. Async everywhere
Every I/O call is `await`. No blocking calls in the request path. Heavy CPU? Run in `asyncio.to_thread`.

### 4. Strict layering
- Endpoints → Services → Helpers / Core
- Never call HTTP from services
- Never query DB from endpoints

### 5. No `print`
Use `log = get_logger(__name__)`. Logs are structured JSON.

### 6. Errors = `AppError` subclasses
Never `raise Exception(...)` for known cases. The exception handler turns `AppError` into clean JSON.

### 7. Migrations are append-only
Once merged, never edit. Always include `downgrade()`.

### 8. Pydantic for boundaries
Schemas at HTTP edges and at LLM JSON edges. ORM models stay internal.

## Tests

```bash
# All tests
pytest -q

# Just unit
pytest -q app/tests/unit

# With coverage
pytest --cov=app --cov-report=term-missing
```

### Layout

```
app/tests/
├── conftest.py              # fixtures (env defaults, dummy keys)
├── unit/                    # pure logic, no DB / network
│   ├── test_encryption.py
│   ├── test_jwt.py
│   └── test_template_engine.py
├── integration/             # touches DB / Redis (testcontainers ideal)
└── e2e/                     # full HTTP path, async client
```

### Mocking external services

- **OpenAI**: `respx` to stub HTTP
- **Redis**: real, on a different DB index
- **Postgres**: real, run migrations into a temp schema then drop

## Lint / format / typecheck

```bash
ruff check .
ruff format .
mypy app
```

CI runs these on every PR (`.github/workflows/`).

### Ruff config (in `pyproject.toml`)

- `line-length=100`
- Selected: `E F W I B UP S ASYNC C4 RET SIM`
- Tests are allowed to use `assert`, plain hardcoded passwords (`S101`, `S105`, `S106`)

### mypy

`strict = true` everywhere except migrations. Add type hints; don't silence errors with `# type: ignore` unless you explain why in a comment.

## Adding a feature — checklist

1. **Write the schema** in `app/schemas/<entity>.py`
2. **Add DB model + migration** if needed
3. **Implement service method** in `app/services/<entity>_service.py` with tenant filter + RBAC
4. **Wire endpoint** in `app/api/v1/endpoints/<entity>.py`
5. **Add to router**: `app/api/v1/router.py`
6. **Tests** at minimum:
    - Unit test for service business rules
    - Integration test for the endpoint (HTTP path + auth + tenant isolation)
7. **Update docs** if it's a public-facing change

## Adding a SQL template

1. Add JSON entry to `app/query_engine/templates/query_templates.json`
2. The LLM intent extractor automatically picks it up via `template_loader.list_templates()`
3. Add a test in `app/tests/unit/test_template_engine.py` that:
    - Loads it
    - Validates required params reject bad input
    - Round-trips bound SQL through the parameter binder

## Adding an LLM prompt

1. New prompt file in `app/llm/prompts/<name>.txt`
2. New generator file `app/llm/<name>_generator.py` with one async function
3. Mock with `respx` in tests

## Debugging tips

| Problem | Check |
|---------|-------|
| 401 on every request | `SECRET_KEY` matches between issuer and verifier (same env across instances) |
| Can't decrypt connection password | `ENCRYPTION_KEY` changed — old ciphertext is now unreadable. Plan key rotation carefully. |
| Slow queries | Set `DEBUG=true` to log SQL; check pool size; check target DB indexes |
| WebSocket drops | Check load balancer idle timeout (`uvicorn --ws-ping-interval`); add ping/pong |
| LLM returns invalid JSON | Lower temperature; require `response_format={"type":"json_object"}`; validate with Pydantic |
| Rate limit too aggressive | Tune via env vars; check `Retry-After` header in response |

## Git workflow

- **`main`** = production-deployable, protected
- Feature branches: `feature/<short>` or `fix/<short>`
- PRs require: passing CI (lint + typecheck + tests), 1 review
- Squash-merge to `main`
- Release tags: semver `v1.2.3`

## Releases

```bash
# Tag a release
git tag -a v1.0.0 -m "Initial release"
git push --tags
```

CI builds an image tagged with the version + `latest`.

## Useful commands

```bash
# Pretty-print JSON logs in dev
uvicorn app.main:app --reload | jq .

# Generate a new alembic revision (autogenerate)
alembic -c app/migrations/alembic.ini revision --autogenerate -m "your slug"

# Run only failed tests
pytest --lf

# Profile a hot path
python -m cProfile -o out.prof -m uvicorn app.main:app
snakeviz out.prof

# Check coverage gaps
pytest --cov=app --cov-report=html && open htmlcov/index.html
```

Next → [Security](./08-security.md)
