# 8 — Module: Query Engine (`app/query_engine/`)

The **query_engine** turns a `(template_id, params)` pair into actual rows from the customer's database. It does **not** invoke any LLM. It does:

1. **Load** the template definition (validates SELECT-only, has JSON schema for params)
2. **Bind** parameters safely (no string concat, ever)
3. **Execute** on the target DB with row caps + timeout
4. **Stream** rows back in batches

This module is what makes "LLM picks template" safe: the template controls the SQL, the parameters are bound by driver placeholders.

## File map

```
app/query_engine/
├── template_loader.py             # Load + validate templates JSON
├── parameter_binder.py            # Type coerce + bind into SQL
├── executor.py                    # Stream rows from target DB
└── templates/
    └── query_templates.json       # Curated SQL templates
```

## Template format

`query_templates.json` is an array of template definitions:

```json
[
  {
    "id": "top_customers_by_revenue",
    "description": "Top N customers by revenue in the last X days",
    "sql": "SELECT customer_id, customer_name, SUM(amount) AS revenue FROM orders WHERE created_at >= NOW() - INTERVAL '%(period_days)s days' GROUP BY 1,2 ORDER BY revenue DESC LIMIT %(limit)s",
    "params": {
      "limit": {"type": "int", "min": 1, "max": 100, "default": 10},
      "period_days": {"type": "int", "min": 1, "max": 365, "default": 30}
    },
    "result_columns": ["customer_id", "customer_name", "revenue"]
  },
  {
    "id": "revenue_over_time",
    "description": "Revenue grouped by day/week/month",
    "sql": "SELECT date_trunc('%(granularity)s', created_at) AS bucket, SUM(amount) AS revenue FROM orders WHERE created_at >= NOW() - INTERVAL '%(period_days)s days' GROUP BY 1 ORDER BY 1",
    "params": {
      "granularity": {"type": "enum", "values": ["day","week","month"], "default": "day"},
      "period_days": {"type": "int", "min": 1, "max": 365, "default": 90}
    },
    "result_columns": ["bucket", "revenue"]
  },
  ...
]
```

## `template_loader.py`

### `init_template_registry()`

Called on app startup. Reads `query_templates.json`, validates:

- Each template has `id`, `sql`, `params`
- `sql` starts with `SELECT` (case-insensitive, after stripping comments)
- `sql` does NOT contain `;` (single statement only)
- All `%(name)s` placeholders in SQL have a matching entry in `params`
- Each param's `type` is one of `int`, `float`, `string`, `enum`, `bool`, `date`

### `get(template_id) -> Template`

Returns the loaded template or raises `NotFound`.

### Dialect Translation & Adaptation
To support multi-database environments without maintaining duplicate SQL templates for each dialect, `template_loader.py` contains a dynamic SQL dialect translation engine. If a template has SQL written only for MSSQL (`mssql` key) and the target database is PostgreSQL or CloudSQL, the loader adapts the SQL statements on-the-fly when calling `sql_for(db_type)`:

1. **`SELECT TOP` translation**: Translates `SELECT TOP %(limit)s ...` or `SELECT TOP N ...` into standard SELECT queries with trailing `LIMIT %(limit)s` or `LIMIT N`.
2. **Date function translation**: Rewrites `GETDATE()` to PostgreSQL's `CURRENT_DATE`.
3. **Difference calculator translation**: Rewrites `DATEDIFF(day, dateA, dateB)` to date arithmetic: `((dateB) - (dateA))`.
4. **Coalesce helper translation**: Rewrites `ISNULL(valA, valB)` to standard `COALESCE(valA, valB)`.

This design ensures single-template source-of-truth while remaining cross-compatible across target databases.

### `list_templates() -> list[TemplateMeta]`

Returns id + description + param schema. Used by `intent_extractor` to render the registry into the system prompt.

## `parameter_binder.py`

### `bind(template, params) -> (sql, bound_params)`

Validates user-supplied params against the template's schema, coerces types, then returns the SQL with driver-style placeholders.

```python
sql, params = parameter_binder.bind(template, {"limit": 10, "period_days": 30})
# sql:    "SELECT ... LIMIT $1 ... INTERVAL '$2 days'"  (postgres)
# params: [10, 30]
```

### Validation rules

| Type | Checks |
|------|--------|
| `int` | int castable, within [min, max] |
| `float` | float castable, within [min, max] |
| `string` | str castable, regex match if `pattern` given |
| `enum` | value ∈ `values` list |
| `bool` | bool castable |
| `date` | ISO 8601 parseable |

### Driver-specific placeholder conversion

The template uses `%(name)s` (Python style). The binder converts to:

| DB type | Placeholder |
|---------|-------------|
| postgres | `$1, $2, ...` (asyncpg) |
| mysql | `%s` (aiomysql) |
| mssql | `?` (aioodbc) |
| oracle | `:1, :2, ...` (oracledb) |

### Defense in depth

Even if a malicious template were added, parameter values are bound by the driver, not interpolated as strings. SQL injection via `params` is **structurally impossible**.

## `executor.py`

### `stream(connection, sql, params) -> AsyncIterator[list[dict]]`

```python
async for batch in executor.stream(conn, sql, params):
    # batch is list[dict], size = QUERY_BATCH_SIZE (default 200)
    process(batch)
```

#### What it does

1. Get a pool for `connection` from `target_pool.get_target_pool(connection)`
2. Acquire a connection from the pool
3. Set statement timeout = `QUERY_TIMEOUT_SECONDS`
4. Open a cursor / prepared stmt depending on driver
5. Fetch in batches of `QUERY_BATCH_SIZE`
6. Yield each batch (list[dict])
7. Stop early if total rows ≥ `QUERY_MAX_ROWS`
8. Release pool connection

#### Error mapping

| Driver error | Mapped to |
|--------------|-----------|
| Statement timeout | `ExecutionError("rate_limited")` |
| Permission denied | `ExecutionError("error", "permission_denied")` |
| Network reset | `ExecutionError("error", "connection_lost")` |
| Other | `ExecutionError("error", original_msg)` |

## Adding a new template

Just add to `query_templates.json`:

```json
{
  "id": "stale_orders",
  "description": "Orders not updated in N days",
  "sql": "SELECT id, customer_id, status, updated_at FROM orders WHERE updated_at < NOW() - INTERVAL '%(days)s days' ORDER BY updated_at LIMIT %(limit)s",
  "params": {
    "days": {"type": "int", "min": 1, "max": 365, "default": 30},
    "limit": {"type": "int", "min": 1, "max": 500, "default": 100}
  },
  "result_columns": ["id", "customer_id", "status", "updated_at"]
}
```

That's it. The template:

- Becomes available via `template_loader`
- Is described to the LLM in `intent_extractor`'s system prompt
- Can be referenced by `template_id` in saved Reports
- Is testable via `app/tests/unit/test_template_engine.py`

## Why not free-form SQL?

A common ask: "let the LLM write SQL directly, why limit it?"

| Concern | Free-form SQL | Curated templates |
|---------|---------------|-------------------|
| Prompt injection → exfiltration | ⚠️ Hard to fully prevent | ✅ Structurally impossible |
| Multi-DB compatibility | ⚠️ LLM dialect drift | ✅ Author per-DB if needed |
| Performance | ⚠️ Bad plans, full scans | ✅ Vetted query plans |
| Result schema | ⚠️ Unknown | ✅ Declared `result_columns` |
| RBAC at row level | ⚠️ Hard | ✅ Easy in template |
| Audit | ⚠️ Variable SQL per call | ✅ Stable template_id |

The trade-off is: you hand-curate templates. For most analytics SaaS use cases this is the right call. For a totally open self-serve BI tool, you'd add a separate "raw SQL" code path with stricter sandboxing (read-only roles, query timeout, row limits, output redaction).

## Testing

`app/tests/unit/test_template_engine.py` covers:

- All starter templates load
- Each rejects bad params
- SQL placeholders are converted correctly per driver
- Bound params survive driver round-trip

Next → [Module: Schemas](./04-module-schemas.md)
