# 3 — Architecture

## System Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLIENT (Browser/SPA)                     │
└──────────────┬───────────────────────────┬──────────────────────┘
               │ REST (JSON)                │ WebSocket (JSON msgs)
               ▼                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                        FASTAPI APP                              │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Middleware: CORS · RequestId · Exception handlers      │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌─────────────────┐  ┌──────────────────┐  ┌──────────────┐   │
│  │  api/v1/        │  │  ws router       │  │  health      │   │
│  │  endpoints/     │  │  (sessions ws)   │  │              │   │
│  └────────┬────────┘  └─────────┬────────┘  └──────────────┘   │
│           │ Depends(...)         │                              │
│           ▼                      ▼                              │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  dependencies: auth · tenancy · rate_limit              │   │
│  └────────┬────────────────────────────────────────────────┘   │
│           ▼                                                    │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  services/   (business logic, one per aggregate)        │   │
│  │  • auth_service  • user_service  • invitation_service   │   │
│  │  • connection_service  • session_service                │   │
│  │  • query_service  • report_service  • dashboard_service │   │
│  │  • websocket_manager                                    │   │
│  └────────┬────────────────────────────────────────────────┘   │
│           │                                                    │
│           ├──────────────┬──────────────┬───────────────┐     │
│           ▼              ▼              ▼               ▼     │
│      ┌────────┐    ┌──────────┐  ┌──────────────┐  ┌───────┐ │
│      │ llm/   │    │ query_   │  │  core/       │  │ utils │ │
│      │        │    │ engine/  │  │              │  │       │ │
│      └───┬────┘    └────┬─────┘  └──────┬───────┘  └───────┘ │
│          │              │               │                    │
│          ▼              ▼               ▼                    │
│      OpenAI         Templates    DB · Redis · Logging        │
└─────────────────────────────────────────────────────────────────┘
                          │                │
                          ▼                ▼
                  ┌──────────────┐  ┌──────────────┐
                  │  POSTGRES    │  │  REDIS       │
                  │  (metadata)  │  │  (cache+RL)  │
                  └──────────────┘  └──────────────┘
                          │
                          ▼
                  ┌──────────────────────┐
                  │  TARGET DBs (per org)│
                  │  PG/MySQL/MSSQL/Oracle│
                  └──────────────────────┘
```

## Layered Architecture

The app follows a strict 4-layer architecture. Lower layers know nothing about upper layers.

### Layer 1: API (`app/api/`)
- HTTP endpoints + WebSocket routes
- Request validation via Pydantic schemas
- Response serialization
- **No business logic** — just receives request, calls service, returns response

### Layer 2: Services (`app/services/`)
- All business logic lives here
- Each service owns one aggregate (auth, sessions, queries, reports...)
- Talks to: DB models, Redis, LLM, query engine
- **Tenancy enforcement** — every query filters by `org_id`

### Layer 3: Domain helpers
- `app/llm/` — wraps OpenAI, prompt templates
- `app/query_engine/` — template loader, parameter binder, executor
- These are stateless utilities used by services

### Layer 4: Core (`app/core/`)
- Cross-cutting concerns: config, logging, exceptions
- DB engine, Redis client, target DB pool registry
- Security primitives (JWT, Fernet, bcrypt)
- Rate limiter

## Multi-Tenancy Model

### How tenancy is enforced

```python
# Every service method receives ctx (org_id, user_id, role)
async def list_connections(db: AsyncSession, ctx: TenantCtx):
    stmt = select(DBConnection).where(
        DBConnection.org_id == ctx.org_id   # ← MUST be present
    )
    return (await db.execute(stmt)).scalars().all()
```

### `TenantCtx` flows through the request

```
Request → JWT middleware → resolves user
         → tenancy dep → loads ctx (org_id, user_id, role)
         → service receives ctx
         → every DB query filters by ctx.org_id
```

### Cross-tenant access prevention

| Mechanism | Where |
|-----------|-------|
| `org_id` filter | All service queries |
| FK constraint | DB schema (e.g., `gi_sessions.org_id → organizations.id`) |
| RBAC check | Service layer (admin/editor/viewer) |
| Rate limit key | `rate:org:{org_id}:user:{user_id}` |
| Audit log field | `request.org_id` in JSON logs |

## Request Lifecycle (REST)

```
1. Request arrives → RequestIdMiddleware injects x-request-id
2. CORS check
3. Route matched in api/v1/router.py
4. Depends(auth) extracts JWT → User
5. Depends(tenancy) resolves TenantCtx
6. Depends(rate_limit) checks token bucket in Redis
7. Endpoint handler called → calls service method
8. Service: business logic + DB queries (all org-scoped)
9. Pydantic schema serializes response
10. RequestIdMiddleware adds x-request-id to response headers
```

## Request Lifecycle (WebSocket — query streaming)

```
1. Client opens WS to /api/v1/ws/sessions/{session_id}?token=...
2. Auth + tenancy resolved from query string token
3. WebSocketManager registers connection
4. Client sends {"type": "run_query", "prompt": "..."}
5. Server emits status: "extracting_intent"
   → llm.intent_extractor → IntentResult(template_id, params)
6. Server emits sql: "SELECT ..."
   → query_engine.template_loader.get(template_id)
   → query_engine.parameter_binder.bind(template, params)
7. Server emits status: "executing"
   → query_engine.executor stream rows in batches
   → for each batch: emit data: {"rows": [...]}
8. Server emits status: "generating_insight"
   → llm.insight_generator(rows, intent) → text
   → emit insight: {"text": "..."}
9. Server emits complete
10. WebSocketManager unregisters; QueryHistory persisted
```

## Data Model (13 tables)

```
organizations ──┬──< users
                ├──< db_connections ──< db_connection_access >── users
                ├──< gi_sessions ──< query_history
                ├──< reports ──< report_columns
                └──< dashboards ──< dashboard_reports >── reports
```

| Table | Purpose |
|-------|---------|
| `organizations` | Tenant root. plan_type, owner_id |
| `users` | Members of an org. role, status |
| `db_connections` | Customer's target DBs (encrypted creds) |
| `db_connection_access` | Per-user grants (null user = org-wide) |
| `gi_sessions` | Chat-style query sessions with rolling context |
| `query_history` | Every executed query (audit + replay) |
| `reports` | Saved query template + params |
| `report_columns` | Per-report column display config |
| `dashboards` | Containers for reports |
| `dashboard_reports` | M2M with layout (x, y, w, h) |

See [Database module](./04-module-database.md) for full schema.

## Concurrency & Scale

- **Async-first**: every I/O is `await`-able. No sync DB drivers.
- **Connection pooling**:
  - Metadata DB: SQLAlchemy async pool (configurable size)
  - Target DBs: per-connection LRU cache of asyncpg/aiomysql/aioodbc pools
- **Rate limiting**: Redis token bucket via Lua script (atomic)
- **WebSocket scale**: in-process registry by default; pluggable to Redis pub/sub for multi-instance

## Gateway Agent Architecture & Scalability

For enterprise customers whose databases (e.g. ERP MSSQL, Oracle, local Postgres) are hosted behind private firewalls or on-premise intranets, Repnex uses an **outbound-only Gateway Agent** method.

### How it works:
1. **Outbound Connection**: The local agent (`repnex-agent.py`) initiates a secure outbound WebSocket connection to the Repnex Cloud at `/api/v1/ws/gateway?agent_name=...&token=...`. Since the connection is outbound, the customer's network admin **does not need to open any inbound ports** or modify firewall rules.
2. **Persistent Registry**: The cloud `GatewayManager` registers the WebSocket connection under the key `{org_id}:{agent_name}` in an in-memory registry.
3. **Query Dispatch**: When a user queries a database with host `gateway:agent_name`, the cloud server compiles the parameter-bound SQL query and sends it as a JSON payload over the established WebSocket.
4. **Local Execution**: The agent executes the SQL locally against the on-premise database using its local connection pool, formats the raw result into JSON, and returns the response over the WebSocket.
5. **Response Resolution**: The `GatewayManager` matches the incoming response's `query_id` to a pending `asyncio.Future`, resolves the query, and streams the rows back to the frontend.

### Scalability Limits:
- **Connection Scale**: A single backend node can handle **1,000 to 5,000 concurrent active agents** (limited only by memory and OS file descriptor limits). To scale to tens of thousands of agents across multiple instances, the `GatewayManager` and `WebSocketManager` can be backed by Redis Pub/Sub, allowing queries to route across different nodes.
- **Eviction & Safety**: To protect the backend from resource exhaustion, a maximum of 5 concurrent WebSocket connections are allowed per user session, and warnings are logged when total active connections exceed 500. Stale agent connections are automatically detected and cleaned up before queries execute.
- **Is this the best method?** Yes. Compared to SSH tunnels or IP whitelisting, the outbound WebSocket agent is zero-config, highly secure, compliant with corporate IT firewalls, and enables instant self-serve onboarding.

## Failure Matrix

| Failure | Behavior |
|---------|----------|
| Target DB down | `ExecutionError("rate_limited" or "error")` → status update |
| LLM rate limited | Retry with exponential backoff (tenacity) |
| Redis down | Rate limiter fails open (configurable); critical paths fail |
| Metadata DB down | App fails healthcheck → orchestrator restarts |
| Token expired | 401, client refreshes via `/auth/refresh` |
| Org over plan | 402 (configurable) |

Next → [Module: Core](./04-module-core.md)
