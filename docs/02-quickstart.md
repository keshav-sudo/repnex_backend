# 2 — Quickstart

Get the backend running locally in **5 minutes**.

## Prerequisites

- Docker + Docker Compose
- An OpenAI API key

## Option A — Docker Compose (Recommended)

### 1. Clone & configure

```bash
git clone https://github.com/keshav-sudo/repnex_backend.git
cd repnex_backend
cp .env.example .env
```

Edit `.env` and set at minimum:

```env
SECRET_KEY=<generate-with: openssl rand -hex 32>
OPENAI_API_KEY=sk-...
ENCRYPTION_KEY=<generate-with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
```

### 2. Start everything

```bash
docker-compose up -d
```

This starts:
- **postgres** on `localhost:5432`
- **redis** on `localhost:6379`
- **backend** on `localhost:8000`

### 3. Run migrations

```bash
docker-compose exec backend alembic -c app/migrations/alembic.ini upgrade head
```

### 4. Verify

```bash
curl http://localhost:8000/api/v1/health
# → {"status":"ok"}
```

Open the **interactive API docs**: http://localhost:8000/docs

---

## Option B — Local Python (without Docker)

### 1. Setup

```bash
python -m venv .venv
source .venv/bin/activate           # Linux/Mac
# .venv\Scripts\activate             # Windows
pip install -r requirements.txt
```

### 2. Start dependencies

```bash
docker run -d --name repnex_pg -p 5432:5432 \
  -e POSTGRES_DB=repnex -e POSTGRES_USER=repnex -e POSTGRES_PASSWORD=repnex \
  postgres:16-alpine

docker run -d --name repnex_redis -p 6379:6379 redis:7-alpine
```

### 3. Configure `.env`

```bash
cp .env.example .env
# Edit DATABASE_URL, REDIS_URL, SECRET_KEY, OPENAI_API_KEY, ENCRYPTION_KEY
```

### 4. Run migrations + start

```bash
alembic -c app/migrations/alembic.ini upgrade head
uvicorn app.main:app --reload --port 8000
```

---

## Smoke Test — End-to-End

### 1. Sign up

```bash
curl -X POST http://localhost:8000/api/v1/auth/signup \
  -H "Content-Type: application/json" \
  -d '{
    "email": "alice@acme.io",
    "password": "Secret123!",
    "org_name": "Acme"
  }'
```

Returns:
```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "user": { "id": "...", "email": "alice@acme.io", "role": "admin" },
  "organization": { "id": "...", "name": "Acme", "plan_type": "free" }
}
```

### 2. Add a database connection

```bash
TOKEN="eyJ..."   # from previous response

curl -X POST http://localhost:8000/api/v1/connections \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Production DB",
    "db_type": "postgres",
    "host": "your-db-host.com",
    "port": 5432,
    "db_name": "production",
    "username": "readonly_user",
    "password": "...",
    "ssl_enabled": true
  }'
```

### 3. Create a session & ask a question

```bash
SESSION_ID=$(curl -s -X POST http://localhost:8000/api/v1/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"connection_id": "<from-step-2>", "title": "Sales analysis"}' \
  | jq -r '.id')

curl -X POST http://localhost:8000/api/v1/sessions/$SESSION_ID/query \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Show me top 10 customers by revenue last month"}'
```

### 4. Stream via WebSocket

```javascript
const ws = new WebSocket(
  `ws://localhost:8000/api/v1/ws/sessions/${sessionId}?token=${TOKEN}`
);

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  switch (msg.type) {
    case "status": console.log("⏳", msg.message); break;
    case "sql":    console.log("📜", msg.sql); break;
    case "data":   console.log("📊", msg.rows); break;
    case "insight":console.log("💡", msg.text); break;
    case "complete":console.log("✅ done"); ws.close(); break;
  }
};

ws.send(JSON.stringify({
  type: "run_query",
  prompt: "Show top 10 customers by revenue"
}));
```

---

## Common issues

| Problem | Fix |
|---------|-----|
| `Could not connect to database` | Check `DATABASE_URL` in `.env` matches docker-compose service name |
| `OpenAI API error: 401` | Set `OPENAI_API_KEY` in `.env` |
| `Fernet key invalid` | Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `Alembic: Target database is not up to date` | Run `alembic upgrade head` |
| Port `5432` already in use | Stop existing Postgres or change port in `docker-compose.yml` |

Next → [Architecture](./03-architecture.md)
