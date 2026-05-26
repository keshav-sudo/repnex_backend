# 9 — Module: Schemas (`app/schemas/`)

The **schemas** module contains all Pydantic v2 models used for:

- HTTP request body validation
- HTTP response serialization
- WebSocket message types
- Internal domain DTOs

Pydantic models are the **boundary contract**. ORM models stay in `core/database/models.py`; schemas are decoupled.

## File map

```
app/schemas/
├── common.py              # Shared types: TenantCtx-like, pagination, error
├── auth.py                # SignupRequest, LoginRequest, TokenPair, AuthResponse
├── user.py                # UserRead, InviteRequest, RoleUpdateRequest
├── organization.py        # OrgRead, OrgUpdate
├── connection.py          # ConnectionCreate/Update/Read, TestConnectionResponse
├── session.py             # SessionCreate/Update/Read/Detail
├── query.py               # RunQueryRequest, IntentResult, RunQueryResponse, QueryHistoryRead
├── report.py              # ReportCreate/Update/Read, RunReportRequest
├── dashboard.py           # DashboardCreate/Update/Read, DashboardItemAdd
└── websocket.py           # Discriminated union for WS messages
```

## Naming convention

| Suffix | Purpose | Example |
|--------|---------|---------|
| `Create` | POST body | `ConnectionCreate` |
| `Update` | PATCH body (all fields optional) | `ConnectionUpdate` |
| `Read` | GET response | `ConnectionRead` |
| `Detail` | Expanded GET (with related data) | `SessionDetail` |
| `Response` | Wrapper response | `AuthResponse` |
| `Request` | Alternative to body name | `RunQueryRequest` |

## Examples

### Auth

```python
class SignupRequest(BaseModel):
    email: EmailStr
    password: SecretStr = Field(min_length=8)
    org_name: str = Field(min_length=2, max_length=255)

class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int   # seconds

class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    user: UserRead
    organization: OrganizationRead
```

### Connection

```python
class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    db_type: DBType
    host: str
    port: int = Field(ge=1, le=65535)
    db_name: str
    username: str
    password: SecretStr
    ssl_enabled: bool = False

class ConnectionRead(BaseModel):
    id: UUID
    name: str
    db_type: DBType
    host: str
    port: int
    db_name: str
    ssl_enabled: bool
    is_active: bool
    last_tested_at: datetime | None
    created_at: datetime
    # password is NEVER serialized

    model_config = ConfigDict(from_attributes=True)
```

### Query / WebSocket

```python
class RunQueryRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)

class IntentResult(BaseModel):
    template_id: str
    params: dict[str, Any]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str

class RunQueryResponse(BaseModel):
    query_history_id: UUID
    intent: IntentResult
    sql: str
    rows: list[dict[str, Any]]
    insight: str
    execution_time_ms: int
    rows_returned: int
```

### WebSocket discriminated union

```python
class StatusMsg(BaseModel):
    type: Literal["status"]
    stage: str
    message: str | None = None

class SqlMsg(BaseModel):
    type: Literal["sql"]
    sql: str

class DataMsg(BaseModel):
    type: Literal["data"]
    rows: list[dict[str, Any]]
    is_last_batch: bool

class InsightMsg(BaseModel):
    type: Literal["insight"]
    text: str

class CompleteMsg(BaseModel):
    type: Literal["complete"]
    execution_time_ms: int

class ErrorMsg(BaseModel):
    type: Literal["error"]
    code: str
    message: str

ServerMsg = Annotated[
    StatusMsg | SqlMsg | DataMsg | InsightMsg | CompleteMsg | ErrorMsg,
    Field(discriminator="type"),
]
```

## Conventions

- Use `EmailStr` for emails, `SecretStr` for secrets, `UUID` for ids
- Always set `model_config = ConfigDict(from_attributes=True)` on Read models (allows `Read.model_validate(orm_object)`)
- Field constraints (`min_length`, `ge`, `le`, `regex`) declared right on the field
- No business logic in schemas — they validate shape, not state
- Date/time fields use `datetime` with timezone; serialized as ISO 8601

## Why DTOs (not raw ORM)?

| Risk | Cause | Mitigation |
|------|-------|------------|
| Leaking internal cols | `from_orm` returns everything | Explicit Read schemas |
| Coupling API to schema changes | Add column → API breaks | Schema rename mapping |
| Slow lazy loads in serializer | ORM access during JSON write | Pre-fetched Pydantic field |
| `password_hash` in response 😱 | Forgot to strip | Schemas only have public fields |

Next → [Module: Database](./04-module-database.md)
