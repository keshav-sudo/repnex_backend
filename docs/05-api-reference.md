# 11 — API Reference

Base URL: `http://localhost:8000/api/v1`

All authenticated endpoints require `Authorization: Bearer <access_token>`.

> 📜 **Live OpenAPI docs**: open http://localhost:8000/docs after starting the server. The OpenAPI JSON is at `/openapi.json`.

---

## Authentication

### `POST /auth/signup`

Create a new organization with the first admin user.

**Body**
```json
{
  "email": "alice@acme.io",
  "password": "Secret123!",
  "org_name": "Acme"
}
```

**Response (201)**
```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "user": { "id": "...", "email": "alice@acme.io", "role": "admin", "status": "active" },
  "organization": { "id": "...", "name": "Acme", "plan_type": "free" }
}
```

### `POST /auth/login`
**Body**: `{ "email": "...", "password": "..." }` → token pair

### `POST /auth/refresh`
**Body**: `{ "refresh_token": "..." }` → new pair, old refresh blacklisted

### `POST /auth/logout`
**Body**: `{ "refresh_token": "..." }` → 204

### `POST /auth/accept-invite`
**Body**: `{ "invite_token": "...", "password": "..." }` → AuthResponse

---

## Users

| Method | Path | Body | Auth |
|--------|------|------|------|
| GET | `/users/me` | — | any |
| GET | `/users` | — | admin |
| PATCH | `/users/{id}/role` | `{ "role": "editor" }` | admin |
| POST | `/users/me/password` | `{ "old": "...", "new": "..." }` | self |
| POST | `/users/invite` | `{ "email": "...", "role": "editor" }` | admin |
| DELETE | `/users/{id}` | — | admin |

---

## Organizations

| Method | Path | Body |
|--------|------|------|
| GET | `/orgs/me` | — |
| PATCH | `/orgs/me` | `{ "name": "..." }` (admin) |
| GET | `/orgs/me/members` | — |
| GET | `/orgs/me/usage` | — |

---

## Connections

### `POST /connections`
```json
{
  "name": "Production DB",
  "db_type": "postgres",
  "host": "db.acme.com",
  "port": 5432,
  "db_name": "production",
  "username": "readonly",
  "password": "...",
  "ssl_enabled": true
}
```
**Response (201)**: `ConnectionRead` (no password)

### Other connection routes
| Method | Path | Notes |
|--------|------|-------|
| GET | `/connections` | List in org |
| GET | `/connections/{id}` | One |
| PATCH | `/connections/{id}` | Update fields (re-encrypt creds if provided) |
| DELETE | `/connections/{id}` | Soft-delete or hard depending on policy |
| POST | `/connections/{id}/test` | Returns `{ ok, latency_ms, error }` |
| POST | `/connections/{id}/access` | `{ "user_id": null }` = org-wide |
| DELETE | `/connections/{id}/access/{user_id}` | Revoke |

---

## Sessions

### `POST /sessions`
```json
{ "connection_id": "...", "title": "Sales analysis" }
```

### `GET /sessions`
Returns user's sessions, paginated.

### `GET /sessions/{id}`
Returns session + last 50 history items.

### `PATCH /sessions/{id}`
```json
{ "title": "...", "status": "archived" }
```

### `GET /sessions/{id}/history`
Paginated query history for a session.

---

## Query (REST)

### `POST /sessions/{session_id}/query`
```json
{ "prompt": "Top 10 customers by revenue last month" }
```

**Response**
```json
{
  "query_history_id": "...",
  "intent": {
    "template_id": "top_customers_by_revenue",
    "params": { "limit": 10, "period_days": 30 },
    "confidence": 0.95
  },
  "sql": "SELECT ...",
  "rows": [ ... ],
  "insight": "Acme Inc leads with $1.2M; revenue grew 23% vs prior month.",
  "execution_time_ms": 1845,
  "rows_returned": 10
}
```

For real-time streaming, use the WebSocket endpoint (below) instead.

---

## WebSocket

### `WS /ws/sessions/{session_id}?token=<access_token>`

**Client → Server**
```json
{ "type": "run_query", "prompt": "..." }
{ "type": "cancel" }
```

**Server → Client** (each message is a discrete event)
```json
{ "type": "status",   "stage": "extracting_intent" }
{ "type": "sql",      "sql": "SELECT ..." }
{ "type": "data",     "rows": [...], "is_last_batch": false }
{ "type": "data",     "rows": [...], "is_last_batch": true }
{ "type": "insight",  "text": "..." }
{ "type": "complete", "execution_time_ms": 1234 }
{ "type": "error",    "code": "rate_limited", "message": "..." }
```

---

## Reports

### `POST /reports`
```json
{
  "name": "Top customers (last 30 days)",
  "description": "...",
  "query_template_id": "top_customers_by_revenue",
  "parameters": { "limit": 25, "period_days": 30 },
  "is_public": false
}
```

| Method | Path | Notes |
|--------|------|-------|
| GET | `/reports` | List (own + public in org) |
| GET | `/reports/{id}` | Detail with columns |
| PATCH | `/reports/{id}` | Update |
| DELETE | `/reports/{id}` | Delete |
| PATCH | `/reports/{id}/pin` | Toggle pinning status of a report |
| POST | `/reports/{id}/run` | Execute report immediately (ad-hoc) |
| PATCH | `/reports/{id}/schedule` | Configure or clear the background auto-refresh interval |
| POST | `/reports/{id}/refresh` | Manually run and store result in a new ReportSnapshot |
| GET | `/reports/{id}/snapshots` | List run snapshots (metadata only, paginated) |
| GET | `/reports/{id}/snapshots/{sid}` | Retrieve specific snapshot detail including raw row data |
| POST | `/reports/{id}/columns` | Add column configuration |
| PATCH | `/reports/{id}/columns/{col_id}` | Update column |
| DELETE | `/reports/{id}/columns/{col_id}` | Remove column |

### `POST /reports/{id}/run`
```json
{ "override_params": { "period_days": 7 } }
```
Returns rows + columns (no LLM step).

### `PATCH /reports/{id}/schedule`
Configure auto-refresh intervals. Pass `interval_days=0` or `null` to disable auto-refresh.
```json
{
  "interval_days": 3,
  "auto_refresh_connection_id": "8b51d8b7-4c28-444f-9556-9a2cf1114b0b"
}
```

### `POST /reports/{id}/refresh`
```json
{
  "connection_id": "8b51d8b7-4c28-444f-9556-9a2cf1114b0b"
}
```
Returns the generated `SnapshotDetailRead` representation including rows.

### `GET /reports/{id}/snapshots/{snapshot_id}`
Returns full historical snapshot details:
```json
{
  "id": "7fae2a04-94ef-4b47-8149-a6873328e1aa",
  "report_id": "d047321a-7b3b-4889-bb0e-1aa38bc74e99",
  "run_status": "success",
  "rows_returned": 2,
  "execution_time_ms": 142,
  "raw_data": [
    { "Customer": "1001", "CustomerName": "Acme Corp", "CurrentBalance": 50000 },
    { "Customer": "1002", "CustomerName": "Beta LLC", "CurrentBalance": 25000 }
  ],
  "error_message": null,
  "created_at": "2026-06-12T10:44:20Z"
}
```

---

## Dashboards

| Method | Path | Notes |
|--------|------|-------|
| GET | `/dashboards` | List |
| POST | `/dashboards` | Create `{ name, is_default? }` |
| GET | `/dashboards/{id}` | Detail with items |
| PATCH | `/dashboards/{id}` | Update |
| DELETE | `/dashboards/{id}` | Delete |
| POST | `/dashboards/{id}/items` | Add report tile `{ report_id, x, y, w, h }` |
| PATCH | `/dashboards/{id}/items/{report_id}` | Move/resize |
| DELETE | `/dashboards/{id}/items/{report_id}` | Remove tile |

---

## Health

| Method | Path | Returns |
|--------|------|---------|
| GET | `/health/live` | `{ "status": "ok" }` always (200 if process alive) |
| GET | `/health/ready` | 200 if DB + Redis reachable; else 503 |

---

## Admin

| Method | Path | Notes |
|--------|------|-------|
| GET | `/admin/audit` | Recent audit events for org |
| POST | `/admin/key-rotation` | Rotate org's encryption key (advanced) |

---

## Error format

All error responses share this shape:

```json
{
  "code": "not_found",
  "message": "Connection not found",
  "request_id": "8c3a..."
}
```

| Code | HTTP | Cause |
|------|------|-------|
| `not_authenticated` | 401 | Missing / bad / expired token |
| `forbidden` | 403 | Wrong role / cross-tenant |
| `not_found` | 404 | Resource missing or not in org |
| `validation_failed` | 422 | Pydantic validation |
| `rate_limited` | 429 | Token bucket empty (returns `Retry-After`) |
| `conflict` | 409 | Unique constraint violation |
| `internal_error` | 500 | Unhandled |

---

## Rate Limits (defaults)

| Endpoint | Capacity | Refill |
|----------|----------|--------|
| `POST /auth/login` | 10 | 1/s |
| `POST /auth/signup` | 5 | 0.1/s |
| `POST /sessions/{id}/query` | 20 | 0.5/s |
| WebSocket queries | Same as `query` (counted per message) |
| All others | 100 | 2/s |

Configurable via env vars; see `.env.example`.

Next → [Deployment](./06-deployment.md)
