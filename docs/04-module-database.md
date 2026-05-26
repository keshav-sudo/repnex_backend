# 10 — Module: Database (`app/core/database/` + `app/migrations/`)

## Overview

The database module is split:

- **Models** — `app/core/database/models.py` (SQLAlchemy 2.0 ORM)
- **Engine + Session** — `app/core/database/session.py`
- **Target pool** — `app/core/database/target_pool.py`
- **Migrations** — `app/migrations/` (Alembic, async)

This doc focuses on the **schema**.

## Schema (13 tables, 6 enums)

### Enums

| Name | Values |
|------|--------|
| `plan_type` | `free`, `pro`, `enterprise` |
| `user_role` | `admin`, `editor`, `viewer` |
| `user_status` | `pending`, `active`, `expired` |
| `db_type` | `postgres`, `mysql`, `mssql`, `oracle`, `cloudsql` |
| `session_status` | `active`, `archived` |
| `execution_status` | `success`, `error`, `rate_limited` |

### Tables

#### `organizations` — tenant root
```
id           UUID PK
name         VARCHAR(255) UNIQUE
owner_id     UUID FK→users.id (SET NULL)        ← circular FK, set after users insert
plan_type    plan_type
created_at   TIMESTAMPTZ DEFAULT NOW()
```

#### `users` — members
```
id              UUID PK
org_id          UUID FK→organizations.id (CASCADE)
email           VARCHAR(320)
hashed_password VARCHAR(255) NULL    ← null until invite accepted
role            user_role
invited_by      UUID FK→users.id (SET NULL)
status          user_status
created_at      TIMESTAMPTZ DEFAULT NOW()

UNIQUE(org_id, email)                ← email unique per org, not globally
INDEX(org_id)
```

#### `db_connections` — customer's databases
```
id                 UUID PK
org_id             UUID FK→organizations.id (CASCADE)
created_by         UUID FK→users.id (RESTRICT)
name               VARCHAR(255)
db_type            db_type
host               VARCHAR(255)
port               INT
db_name            VARCHAR(255)
encrypted_username TEXT          ← Fernet ciphertext
encrypted_password TEXT          ← Fernet ciphertext
ssl_enabled        BOOL DEFAULT false
is_active          BOOL DEFAULT true
last_tested_at     TIMESTAMPTZ NULL
created_at         TIMESTAMPTZ DEFAULT NOW()

INDEX(org_id)
```

#### `db_connection_access` — per-user grants
```
id            UUID PK
connection_id UUID FK→db_connections.id (CASCADE)
user_id       UUID FK→users.id (CASCADE) NULL   ← NULL = org-wide
org_id        UUID FK→organizations.id (CASCADE)
granted_by    UUID FK→users.id (RESTRICT)
created_at    TIMESTAMPTZ DEFAULT NOW()

UNIQUE(connection_id, user_id)
```

#### `gi_sessions` — GenAI chat sessions
```
id              UUID PK
user_id         UUID FK→users.id (CASCADE)
org_id          UUID FK→organizations.id (CASCADE)
connection_id   UUID FK→db_connections.id (CASCADE)
title           VARCHAR(255)
context_window  JSONB DEFAULT '[]'    ← list of {role, content} dicts
token_count     INT DEFAULT 0
status          session_status
created_at      TIMESTAMPTZ DEFAULT NOW()

INDEX(user_id)
```

#### `query_history` — every query
```
id                       UUID PK
session_id               UUID FK→gi_sessions.id (CASCADE)
user_id                  UUID FK→users.id (CASCADE)
connection_id            UUID FK→db_connections.id (CASCADE)
natural_language_input   TEXT
generated_sql            TEXT NULL
row_size                 BOOL DEFAULT false
intent                   JSONB DEFAULT '{}'    ← {template_id, params, confidence}
execution_status         execution_status
error_message            TEXT NULL
execution_time_ms        INT NULL
rows_returned            INT NULL
created_at               TIMESTAMPTZ DEFAULT NOW()

INDEX(session_id)
```

#### `reports` — saved queries
```
id                 UUID PK
org_id             UUID FK→organizations.id (CASCADE)
created_by         UUID FK→users.id (RESTRICT)
name               VARCHAR(255)
description        TEXT NULL
query_template_id  VARCHAR(128)         ← references template in JSON
parameters         JSONB DEFAULT '{}'
is_public          BOOL DEFAULT false   ← shared with org
created_at         TIMESTAMPTZ DEFAULT NOW()

INDEX(org_id)
```

#### `report_columns` — column display config
```
id            UUID PK
report_id     UUID FK→reports.id (CASCADE)
column_name   VARCHAR(128)         ← matches template.result_columns
display_name  VARCHAR(128)
position      INT
is_visible    BOOL DEFAULT true
data_type     VARCHAR(32)          ← 'number', 'currency', 'date', 'string', ...
format_config JSONB DEFAULT '{}'   ← {"decimals":2, "currency":"USD", ...}
```

#### `dashboards`
```
id            UUID PK
org_id        UUID FK→organizations.id (CASCADE)
created_by    UUID FK→users.id (RESTRICT)
name          VARCHAR(255)
is_default    BOOL DEFAULT false
layout_config JSONB DEFAULT '{}'
created_at    TIMESTAMPTZ DEFAULT NOW()

INDEX(org_id)
```

#### `dashboard_reports` — m2m with layout
```
id           UUID PK
dashboard_id UUID FK→dashboards.id (CASCADE)
report_id    UUID FK→reports.id (CASCADE)
position_x   INT DEFAULT 0
position_y   INT DEFAULT 0
width        INT DEFAULT 4
height       INT DEFAULT 4
added_at     TIMESTAMPTZ DEFAULT NOW()

UNIQUE(dashboard_id, report_id)
```

## Migrations (Alembic, async)

Files in `app/migrations/versions/`. Naming convention:

```
20250101_0000_0001_initial.py
YYYYMMDD_HHmm_<rev>_<slug>.py
```

### Run migrations

```bash
# Upgrade to latest
alembic -c app/migrations/alembic.ini upgrade head

# Generate new from model changes
alembic -c app/migrations/alembic.ini revision --autogenerate -m "add widgets table"

# Downgrade one step (avoid in prod!)
alembic -c app/migrations/alembic.ini downgrade -1
```

### Rules

- **Migrations are append-only** in production. Never edit a merged migration.
- Always include both `upgrade()` and `downgrade()`.
- For data migrations, use a separate migration after the schema change.
- Test migrations on a copy of prod data before merging.

### Async env

`migrations/env.py` runs migrations through the async engine — same `DATABASE_URL` as the app. No separate sync URL needed.

## Engine + Session

```python
# app/core/database/session.py

engine: AsyncEngine | None = None
AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None

def init_engine() -> None:
    global engine, AsyncSessionLocal
    settings = get_settings()
    engine = create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,
        echo=settings.DEBUG,
    )
    AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
```

`get_db()` is the request-scoped dependency:

```python
async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

## Target pool registry

See [Module: Core](./04-module-core.md#databasetarget_poolpy--per-connection-pool-registry).

## ER diagram

```
            ┌─────────────────┐
            │  organizations  │
            └────────┬────────┘
                     │ 1
            ┌────────┴───────────────────────────────────┐
            │ ▼ N                                          │
       ┌────┴─────┐                                        │
       │  users   │◄───────┐                               │
       └────┬─────┘        │                               │
            │              │ granted_by                    │
            ▼ 1            │                               │
   ┌──────────────────────┐│                               │
   │  db_connections      ├┘                               │
   └─────┬────────────────┘                                │
         │                                                 │
   ┌─────┴───────────────┐                                 │
   ▼ N                   │                                 │
┌────────────────────┐ ┌─────────────────────┐             │
│ db_connection_     │ │  gi_sessions        │             │
│ access             │ └──────┬──────────────┘             │
└────────────────────┘        │                            │
                              ▼ N                          │
                       ┌────────────────┐                  │
                       │ query_history  │                  │
                       └────────────────┘                  │
                                                           │
                                                           ▼ N
                                                    ┌─────────────┐
                                                    │  reports    │
                                                    └──────┬──────┘
                                                           │ 1
                                                ┌──────────┴────────┐
                                                ▼ N                 │
                                       ┌────────────────────┐       │
                                       │  report_columns    │       │
                                       └────────────────────┘       │
                                                                    │
                                                    ┌───────────────┘
                                                    ▼
                                          ┌────────────────────┐
                                          │  dashboards        │
                                          └─────┬──────────────┘
                                                │ 1
                                          ┌─────┴───────────┐
                                          ▼ N
                                  ┌────────────────────┐
                                  │ dashboard_reports  │
                                  └────────────────────┘
```

Next → [API Reference](./05-api-reference.md)
