# 12 — Deployment

## Docker (recommended)

The included `Dockerfile` is multi-stage:

- **Stage 1 (builder)** — installs Python deps into `/install`
- **Stage 2 (runtime)** — slim base, only runtime libs, non-root user, `tini` PID-1

### Build & run

```bash
docker build -t repnex-backend:latest .

docker run -d --name repnex \
  -p 8000:8000 \
  -e DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db \
  -e REDIS_URL=redis://host:6379/0 \
  -e SECRET_KEY=... \
  -e ENCRYPTION_KEY=... \
  -e OPENAI_API_KEY=sk-... \
  -e RUN_MIGRATIONS=true \
  repnex-backend:latest
```

`RUN_MIGRATIONS=true` makes the container run `alembic upgrade head` before starting uvicorn.

### Healthcheck

The Dockerfile registers an internal healthcheck:

```
HEALTHCHECK CMD curl -fsS http://localhost:8000/health/live || exit 1
```

Use `/health/ready` for orchestrator readiness probes (it checks DB + Redis).

## docker-compose

For local + simple deployments:

```bash
cp .env.example .env
# edit .env
docker-compose up -d
```

Services: `postgres`, `redis`, `backend`. Volumes persist data between runs.

## Required env vars

| Variable | Notes |
|----------|-------|
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:port/db` (NOT psycopg) |
| `REDIS_URL` | `redis://host:port/0` |
| `SECRET_KEY` | JWT signing key. Generate: `openssl rand -hex 32` |
| `ENCRYPTION_KEY` | Fernet key. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `OPENAI_API_KEY` | `sk-...` |

## Optional env vars (with sensible defaults)

| Variable | Default | Effect |
|----------|---------|--------|
| `APP_ENV` | `development` | `production` for prod (affects log level, debug) |
| `LOG_LEVEL` | `INFO` | One of DEBUG/INFO/WARNING/ERROR |
| `OPENAI_MODEL` | `gpt-4o` | Or `gpt-4o-mini` for cost |
| `OPENAI_TIMEOUT_SECONDS` | `30` | Per-call |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated |
| `DB_POOL_SIZE` | `10` | Metadata DB pool |
| `DB_MAX_OVERFLOW` | `20` | Burst capacity |
| `RATE_LIMIT_REQUESTS_PER_WINDOW` | `100` | Default bucket capacity |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | |
| `QUERY_TIMEOUT_SECONDS` | `30` | Target DB statement timeout |
| `QUERY_MAX_ROWS` | `10000` | Hard cap |
| `QUERY_BATCH_SIZE` | `200` | WebSocket batch size |
| `TARGET_POOL_MAX_CONNECTIONS` | `50` | LRU cache size |
| `TARGET_POOL_IDLE_TTL_SECONDS` | `300` | Idle pool eviction |
| `SESSION_CONTEXT_WINDOW_SIZE` | `10` | Messages kept in session context |
| `GRACEFUL_SHUTDOWN_SECONDS` | `30` | WebSocket drain on SIGTERM |

## Production checklist

- [ ] **Generate fresh SECRET_KEY and ENCRYPTION_KEY** (don't reuse dev)
- [ ] **Set `APP_ENV=production`** — disables debug + verbose logs
- [ ] **Restrict `CORS_ORIGINS`** to your real frontend
- [ ] **Use TLS terminator** (nginx, Cloudflare, ALB) — uvicorn has `--proxy-headers`
- [ ] **Set up a real Postgres** (RDS/Cloud SQL/managed) with backups
- [ ] **Set up a real Redis** (Elasticache/Memorystore) with persistence
- [ ] **Run migrations as separate step** (CI/CD), not via `RUN_MIGRATIONS=true` for blue/green
- [ ] **Monitor** `/health/ready` from your orchestrator
- [ ] **Centralize logs** — JSON to stdout → ship to Datadog / Loki / Cloudwatch
- [ ] **Rotate `ENCRYPTION_KEY`** periodically (stretch goal — needs re-encrypt job)
- [ ] **Enable Redis password** + TLS in prod
- [ ] **Tune `DB_POOL_SIZE`** based on instance count: `pool_size * instances < db_max_connections`
- [ ] **Set up secret management** — don't bake secrets into images. Use AWS SSM / Vault / GCP Secret Manager.

## Kubernetes example

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: repnex-backend
spec:
  replicas: 3
  selector: { matchLabels: { app: repnex-backend } }
  template:
    metadata: { labels: { app: repnex-backend } }
    spec:
      containers:
      - name: backend
        image: repnex-backend:1.0.0
        ports: [{ containerPort: 8000 }]
        env:
        - name: DATABASE_URL
          valueFrom: { secretKeyRef: { name: repnex-secrets, key: database_url } }
        - name: REDIS_URL
          valueFrom: { secretKeyRef: { name: repnex-secrets, key: redis_url } }
        - name: SECRET_KEY
          valueFrom: { secretKeyRef: { name: repnex-secrets, key: secret_key } }
        - name: ENCRYPTION_KEY
          valueFrom: { secretKeyRef: { name: repnex-secrets, key: encryption_key } }
        - name: OPENAI_API_KEY
          valueFrom: { secretKeyRef: { name: repnex-secrets, key: openai_api_key } }
        - name: APP_ENV
          value: production
        livenessProbe:
          httpGet: { path: /api/v1/health/live, port: 8000 }
          initialDelaySeconds: 10
          periodSeconds: 15
        readinessProbe:
          httpGet: { path: /api/v1/health/ready, port: 8000 }
          initialDelaySeconds: 15
          periodSeconds: 5
        resources:
          requests: { cpu: "200m", memory: "256Mi" }
          limits:   { cpu: "1",    memory: "1Gi" }
```

Apply with `kubectl apply -f`. Add a `Service` + `Ingress` for traffic.

## Migrations in CI/CD

Don't run migrations from app pods. Use a Kubernetes Job (or equivalent) before app rollout:

```yaml
apiVersion: batch/v1
kind: Job
metadata: { name: repnex-migrations-{{ .Values.version }} }
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: migrator
        image: repnex-backend:{{ .Values.version }}
        command: ["alembic", "-c", "app/migrations/alembic.ini", "upgrade", "head"]
        envFrom: [{ secretRef: { name: repnex-secrets } }]
```

Helm/Argo can gate the Deployment rollout on Job success.

## Observability

### Metrics (suggested)

Add `prometheus_client` and expose `/metrics`:

- `repnex_request_duration_seconds{method,path,status}`
- `repnex_query_executions_total{template_id,status}`
- `repnex_llm_calls_total{kind,status}`
- `repnex_target_pool_size{db_type}`

### Tracing

Use OpenTelemetry. The `request_id` middleware already provides a correlation id for log/trace stitching.

### Logging

JSON lines to stdout. Each entry has `timestamp`, `level`, `logger`, `message`, `request_id`, plus any `extra` keys (`org_id`, `user_id`, `query_history_id`, etc.).

Next → [Development](./07-development.md)
