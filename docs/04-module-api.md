# 5 — Module: API (`app/api/`)

The **api** module is the HTTP/WebSocket surface. It does request validation, dependency injection (auth, tenancy, rate limit), and response shaping. **No business logic lives here** — everything is delegated to `app/services/`.

## File map

```
app/api/
└── v1/
    ├── router.py                 # aggregates all routers
    ├── dependencies/
    │   ├── auth.py               # JWT bearer extraction
    │   ├── tenancy.py            # TenantCtx (org_id, user_id, role)
    │   └── rate_limit.py         # Token bucket factory
    └── endpoints/
        ├── auth.py               # /auth/* (signup, login, refresh, logout)
        ├── users.py              # /users/* (me, list, role update)
        ├── organizations.py      # /orgs/* (read, update, members)
        ├── connections.py        # /connections/* (CRUD, test)
        ├── sessions.py           # /sessions/* (CRUD, history)
        ├── query.py              # /query (run a query REST)
        ├── reports.py            # /reports/* (CRUD, run)
        ├── dashboards.py         # /dashboards/* (CRUD, items)
        ├── admin.py              # /admin/* (org/user admin)
        ├── health.py             # /health/* (liveness, readiness)
        └── websocket.py          # /ws/* (real-time query streaming)
```

## `router.py` — composition

Aggregates all endpoint routers under `/api/v1` and includes the WebSocket router separately:

```python
api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(users.router)
# ... etc

ws_router = APIRouter(prefix="/api/v1/ws")
ws_router.include_router(websocket.router)
```

## Dependencies

### `auth.py` — JWT bearer

```python
from app.api.v1.dependencies.auth import get_current_user

@router.get("/users/me")
async def me(user: User = Depends(get_current_user)):
    return user
```

What it does:
1. Extract `Authorization: Bearer <token>` header
2. Decode JWT, verify type == "access"
3. Load user from DB
4. 401 if any step fails

### `tenancy.py` — TenantCtx

```python
@dataclass
class TenantCtx:
    user_id: UUID
    org_id: UUID
    role: UserRole
```

```python
from app.api.v1.dependencies.tenancy import get_tenant_ctx

@router.get("/connections")
async def list_conn(ctx: TenantCtx = Depends(get_tenant_ctx)):
    return await connection_service.list(db, ctx)
```

### `rate_limit.py` — Token bucket factory

```python
from app.api.v1.dependencies.rate_limit import rate_limit

@router.post(
    "/query",
    dependencies=[Depends(rate_limit(capacity=20, refill_per_sec=0.5))],
)
async def run_query(...):
    ...
```

Returns 429 with `Retry-After` header when bucket is empty.

## Endpoint Modules — Quick Reference

### `auth.py` — Authentication

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/auth/signup` | Create new org + admin user |
| POST | `/auth/login` | Email + password → token pair |
| POST | `/auth/refresh` | Rotate refresh → new access + refresh |
| POST | `/auth/logout` | Revoke refresh token (Redis blacklist) |
| POST | `/auth/accept-invite` | Use invite token → activate user |

### `users.py` — Users in org

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/users/me` | Current user profile |
| GET | `/users` | List users in org (admin) |
| PATCH | `/users/{id}/role` | Change role (admin only) |
| POST | `/users/me/password` | Change own password |
| POST | `/users/invite` | Send invite (admin) |
| DELETE | `/users/{id}` | Deactivate user (admin) |

### `organizations.py` — Org management

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/orgs/me` | Current org details |
| PATCH | `/orgs/me` | Update org name |
| GET | `/orgs/me/members` | List org members |
| GET | `/orgs/me/usage` | Plan usage stats |

### `connections.py` — DB connections

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/connections` | List connections in org |
| POST | `/connections` | Create (encrypts creds) |
| GET | `/connections/{id}` | Get one |
| PATCH | `/connections/{id}` | Update |
| DELETE | `/connections/{id}` | Delete |
| POST | `/connections/{id}/test` | Open trial connection |
| POST | `/connections/{id}/access` | Grant user access |
| DELETE | `/connections/{id}/access/{user_id}` | Revoke access |

### `sessions.py` — GenAI sessions

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/sessions` | List sessions (current user) |
| POST | `/sessions` | Create session for a connection |
| GET | `/sessions/{id}` | Session details + history |
| PATCH | `/sessions/{id}` | Update title / archive |
| DELETE | `/sessions/{id}` | Soft-delete |
| GET | `/sessions/{id}/history` | Query history |

### `query.py` — Run a query (REST)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/query` | Run NL query (synchronous) |

For real-time streaming, use the WebSocket endpoint instead.

### `reports.py` — Saved queries

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/reports` | List reports in org |
| POST | `/reports` | Save a report |
| GET | `/reports/{id}` | Get report + columns |
| PATCH | `/reports/{id}` | Update |
| DELETE | `/reports/{id}` | Delete |
| POST | `/reports/{id}/run` | Execute report |
| POST | `/reports/{id}/columns` | Add column config |

### `dashboards.py` — Dashboards

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/dashboards` | List |
| POST | `/dashboards` | Create |
| GET | `/dashboards/{id}` | Detail with reports |
| PATCH | `/dashboards/{id}` | Update layout |
| DELETE | `/dashboards/{id}` | Delete |
| POST | `/dashboards/{id}/items` | Add report tile |
| PATCH | `/dashboards/{id}/items/{rid}` | Update tile position/size |
| DELETE | `/dashboards/{id}/items/{rid}` | Remove tile |

### `admin.py` — Org admin actions

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/admin/audit` | Audit log |
| POST | `/admin/key-rotation` | Rotate encryption key |

### `health.py` — Health checks

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health/live` | Liveness (always 200 if running) |
| GET | `/health/ready` | Readiness (checks DB + Redis) |

### `websocket.py` — Real-time query streaming

| WebSocket | Path | Purpose |
|-----------|------|---------|
| WS | `/api/v1/ws/sessions/{session_id}?token=...` | Stream query execution |

#### Client → Server messages

```json
{"type": "run_query", "prompt": "Show top customers"}
{"type": "cancel"}
```

#### Server → Client messages (discriminated union)

```json
{"type": "status",   "stage": "extracting_intent", "message": "..."}
{"type": "sql",      "sql": "SELECT ..."}
{"type": "data",     "rows": [...], "is_last_batch": false}
{"type": "insight",  "text": "Revenue grew 23% ..."}
{"type": "complete", "execution_time_ms": 1234}
{"type": "error",    "code": "rate_limited", "message": "..."}
```

## Adding a new endpoint

1. **Schema** in `app/schemas/<entity>.py`
2. **Service method** in `app/services/<entity>_service.py`
3. **Endpoint** in `app/api/v1/endpoints/<entity>.py`
4. **Wire** to `app/api/v1/router.py`

```python
# app/api/v1/endpoints/widgets.py
from fastapi import APIRouter, Depends
from app.api.v1.dependencies.tenancy import get_tenant_ctx
from app.services import widget_service
from app.schemas.widget import WidgetCreate, WidgetRead

router = APIRouter(prefix="/widgets", tags=["widgets"])

@router.post("", response_model=WidgetRead, status_code=201)
async def create_widget(
    payload: WidgetCreate,
    db: AsyncSession = Depends(get_db),
    ctx: TenantCtx = Depends(get_tenant_ctx),
) -> WidgetRead:
    return await widget_service.create(db, ctx, payload)
```

## Conventions

- One router file per resource
- Use `response_model=` always (drives OpenAPI + serialization)
- 201 on POST creates, 204 on DELETE
- Always include `Depends(get_tenant_ctx)` unless the route is public
- Never write SQL in endpoint files — call services

Next → [Module: Services](./04-module-services.md)
