# 4 — Module: Core (`app/core/`)

The **core** module is the foundation. It contains everything cross-cutting: config, logging, exceptions, security, database engines, Redis client, rate limiter.

## File map

```
app/core/
├── config.py               # Settings (Pydantic Settings, 50+ env vars)
├── logging.py              # JSON structured logging + redaction
├── exceptions.py           # AppError hierarchy + handlers
├── redis.py                # Redis client lifecycle
├── rate_limiter.py         # Token bucket via Lua script
├── database/
│   ├── base.py             # DeclarativeBase
│   ├── models.py           # 13 SQLAlchemy ORM models
│   ├── session.py          # Async engine + AsyncSessionLocal
│   └── target_pool.py      # Per-connection async pool registry
└── security/
    ├── auth.py             # JWT create/verify (access/refresh/invite)
    ├── encryption.py       # Fernet symmetric encryption
    └── passwords.py        # bcrypt hash/verify
```

## `config.py` — Settings

Pydantic Settings class loaded from environment variables / `.env` file.

```python
from app.core.config import get_settings
settings = get_settings()    # cached, singleton
```

### Key settings groups

| Group | Variables |
|-------|-----------|
| **App** | `APP_ENV`, `DEBUG`, `LOG_LEVEL` |
| **Server** | `HOST`, `PORT`, `CORS_ORIGINS`, `GRACEFUL_SHUTDOWN_SECONDS` |
| **Database** | `DATABASE_URL`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` |
| **Redis** | `REDIS_URL`, `REDIS_POOL_SIZE` |
| **Security** | `SECRET_KEY`, `ENCRYPTION_KEY`, `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS` |
| **OpenAI** | `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_TIMEOUT_SECONDS` |
| **Rate Limit** | `RATE_LIMIT_REQUESTS_PER_WINDOW`, `RATE_LIMIT_WINDOW_SECONDS` |
| **Query** | `QUERY_TIMEOUT_SECONDS`, `QUERY_MAX_ROWS`, `QUERY_BATCH_SIZE` |
| **Target Pool** | `TARGET_POOL_MAX_CONNECTIONS`, `TARGET_POOL_IDLE_TTL_SECONDS` |

### Why `lru_cache` on `get_settings()`?

Reading env vars is fast, but Pydantic validation is not free. Caching makes config reads O(1) after first call.

## `logging.py` — Structured logging

Outputs JSON lines with:
- `timestamp` (UTC ISO)
- `level`
- `logger`
- `message`
- `request_id` (from contextvar)
- `extra` keys

### Sensitive field redaction

A `RedactionFilter` scans messages for keys like `password`, `token`, `api_key`, `secret` and replaces values with `***`.

### Usage

```python
from app.core.logging import get_logger
log = get_logger(__name__)

log.info("user_signup", extra={"user_id": str(user.id), "email": user.email})
```

## `exceptions.py` — Error hierarchy

```python
class AppError(Exception):
    status_code = 500
    code = "internal_error"
    message = "Something went wrong"

class NotAuthenticated(AppError):
    status_code = 401
    code = "not_authenticated"

class Forbidden(AppError):
    status_code = 403
    code = "forbidden"

class NotFound(AppError):
    status_code = 404
    code = "not_found"

class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"

class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"

class ConflictError(AppError):
    status_code = 409
    code = "conflict"
```

`register_exception_handlers(app)` wires these into FastAPI so they return clean JSON:

```json
{ "code": "not_found", "message": "Connection not found", "request_id": "..." }
```

## `redis.py` — Redis client

Lifecycle managed by app `lifespan`:

```python
await init_redis()    # called on startup
client = await get_redis()
await close_redis()   # called on shutdown
```

Uses `redis.asyncio` with connection pool.

## `rate_limiter.py` — Token bucket

Atomic token-bucket algorithm via Redis Lua script. Two-arg key:

```
rate:org:{org_id}:user:{user_id}
```

```python
from app.core.rate_limiter import check_rate_limit

allowed, remaining = await check_rate_limit(
    key=f"rate:org:{ctx.org_id}:user:{ctx.user_id}",
    capacity=100,        # max tokens
    refill_per_sec=2,    # refill rate
    cost=1,              # tokens consumed per call
)
```

Used by `dependencies/rate_limit.py`.

## `database/session.py` — Engine factory

```python
init_engine()                       # creates async engine + AsyncSessionLocal
async with AsyncSessionLocal() as db:
    ...
await dispose_engine()              # close pool on shutdown
```

`get_db()` dependency yields a session per request.

## `database/models.py` — ORM models

13 SQLAlchemy 2.0 models. Highlights:

```python
class Organization(Base):
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    plan_type: Mapped[PlanType]
    # ...

class User(Base):
    id: Mapped[UUID] = ...
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    email: Mapped[str]
    role: Mapped[UserRole]   # admin / editor / viewer
    # uq_users_org_email: UNIQUE(org_id, email)
```

See [Database module doc](./04-module-database.md) for full schema.

## `database/target_pool.py` — Per-connection pool registry

Customers connect their own DBs. We don't want to open a new pool per request — that would melt the target DB.

### Strategy

- `TargetPoolRegistry` is a singleton dict: `connection_id → pool`
- Each pool is `asyncpg.Pool`, `aiomysql.Pool`, etc. depending on `db_type`
- LRU eviction when count exceeds `TARGET_POOL_MAX_CONNECTIONS`
- Idle pools closed after `TARGET_POOL_IDLE_TTL_SECONDS`
- All operations are async-safe (lock per connection_id)

### Usage

```python
from app.core.database.target_pool import get_target_pool

pool = await get_target_pool(connection)   # connection: DBConnection model
async with pool.acquire() as conn:
    rows = await conn.fetch("SELECT ...")
```

## `security/auth.py` — JWT

3 token types:
- **access** — short-lived (15 min default), API calls
- **refresh** — long-lived (7 days default), token rotation
- **invite** — one-time, includes invite_id and email

```python
from app.core.security.auth import create_access_token, decode_token

token = create_access_token(user_id, org_id, role)
payload = decode_token(token, expected_type="access")
```

### Refresh rotation

When `/auth/refresh` is called:
1. Verify old refresh token
2. Mark it as used in Redis (jti blacklist)
3. Issue new access + refresh
4. Old refresh cannot be reused (replay protection)

## `security/encryption.py` — Fernet

AES-128 in CBC mode + HMAC-SHA256 (Fernet spec). Used to encrypt DB connection passwords/usernames before storing in `db_connections` table.

```python
from app.core.security.encryption import encrypt, decrypt

ciphertext = encrypt("p@ssw0rd")        # bytes → str (urlsafe-b64)
plaintext = decrypt(ciphertext)
```

`ENCRYPTION_KEY` env var is the Fernet key. Generate with:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## `security/passwords.py` — bcrypt

```python
from app.core.security.passwords import hash_password, verify_password

hashed = hash_password("Secret123!")    # bcrypt cost=12
ok = verify_password("Secret123!", hashed)
```

## What goes where?

| Need | Place |
|------|-------|
| New env var | `config.py` Settings class |
| New error type | `exceptions.py` (subclass `AppError`) |
| New DB model | `database/models.py` + Alembic migration |
| New cross-cutting helper | `core/` only if used by 2+ modules; else closer to caller |
| Rate limit a new endpoint | Use existing `dependencies/rate_limit.py` factory |

Next → [Module: API](./04-module-api.md)
