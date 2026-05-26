# 14 — Security

Security model in five lines:

1. **Multi-tenant isolation** at every query (`org_id` filter, FK constraints)
2. **Credentials at rest**: Fernet AES-256
3. **Passwords at rest**: bcrypt cost=12
4. **JWT** with refresh rotation + Redis blacklist
5. **No free-form SQL from the LLM** — only parameter-bound templates

## Multi-Tenancy

Every service-layer query filters by `ctx.org_id`. There is no exception. PRs that omit this are blocked.

```python
# Good
stmt = select(DBConnection).where(DBConnection.org_id == ctx.org_id)

# Bad — would leak across tenants
stmt = select(DBConnection).where(DBConnection.id == conn_id)
```

The combined pattern (id + org_id) is required for single-resource lookups:

```python
stmt = select(DBConnection).where(
    DBConnection.id == conn_id,
    DBConnection.org_id == ctx.org_id,
)
```

FK constraints `ON DELETE CASCADE` from `organizations` prevent orphan rows.

## Encryption (data at rest)

Customer DB credentials (`username`, `password`) are encrypted with Fernet before insert. Fernet provides:

- AES-128-CBC with HMAC-SHA256
- IV randomized per encryption
- Time-stamped tokens (rotation possible)

```python
from app.core.security.encryption import encrypt, decrypt

ciphertext = encrypt("p@ssw0rd")     # → urlsafe-b64 string
plaintext  = decrypt(ciphertext)
```

`ENCRYPTION_KEY` must be:

- Generated with `Fernet.generate_key()` (32 random bytes, urlsafe-b64)
- Stored in your secret manager (Vault/SSM/etc.), not in source
- Rotated periodically (requires re-encryption job)

## Passwords

bcrypt at cost factor 12 (≈250ms per verify on modern CPU).

```python
from app.core.security.passwords import hash_password, verify_password

hashed = hash_password("Secret123!")
ok     = verify_password("Secret123!", hashed)
```

Cost 12 is the right balance for 2025: high enough to be expensive for attackers, low enough not to swamp your servers under credential stuffing.

## JWT

### Three token types

| Type | Lifetime | Purpose |
|------|----------|---------|
| `access` | 15 min (default) | Bearer for API + WS |
| `refresh` | 7 days (default) | Trade for new access (rotated) |
| `invite` | 24 hours | Set password to activate user |

### Claims

```json
{
  "sub": "<user_id>",
  "org_id": "<org_id>",
  "role": "admin",
  "type": "access",
  "iat": 1234567890,
  "exp": 1234567890,
  "jti": "<uuid>"
}
```

### Refresh rotation

- New refresh issued each time
- Old refresh's `jti` blacklisted in Redis with TTL = remaining lifetime
- Replay attempt → 401

## Rate limiting

Token bucket algorithm via Redis Lua script (atomic). Key per `(org, user)`.

| Endpoint group | Capacity | Refill |
|----------------|----------|--------|
| Auth | 10 | 1/s |
| Query | 20 | 0.5/s |
| Default | 100 | 2/s |

Returns 429 with `Retry-After: <seconds>`.

## SQL injection — structurally impossible (LLM path)

The path:

```
NL prompt → LLM → IntentResult(template_id, params)
                           │
                           ▼
              template_loader.get(template_id)
                           │
                           ▼
              parameter_binder.bind(template, params)
                           │   (driver placeholders, no string concat)
                           ▼
              executor.stream(connection, sql, params)
```

The LLM **cannot** emit SQL. Even malicious prompts can only:

1. Pick a different template (which has its own constraints)
2. Pass values that the binder will reject (`int` field with `string`, `enum` field with unknown value)

Even a malicious template author can't enable injection: parameters are bound by the driver, not interpolated.

## CORS

`CORS_ORIGINS` env var, comma-separated. Don't use `*` in production.

```env
CORS_ORIGINS=https://app.example.com,https://staging.example.com
```

## Logging redaction

`RedactionFilter` strips values for keys matching:

- `password`, `passwd`, `pwd`
- `token`, `access_token`, `refresh_token`, `invite_token`
- `api_key`, `secret`, `secret_key`, `encryption_key`

Replaced with `***` in JSON output.

## WebSocket auth

Token passed as query string `?token=...` (HTTP headers can't be set on WS in browsers).

Verified once on connect; cached `TenantCtx` for the lifetime of the connection.

## Threat model — quick summary

| Threat | Mitigation |
|--------|------------|
| Cross-tenant data access | `org_id` filter + FK CASCADE |
| Stolen DB cred dump | Fernet encryption at rest |
| Stolen access token | Short TTL (15m) + refresh rotation + jti blacklist |
| Stolen refresh token | Rotation invalidates on replay |
| Brute-force login | Rate limit + bcrypt cost 12 |
| Credential stuffing | Rate limit per (org,user) + per-IP |
| SQL injection from LLM | Template-only, parameter-bound |
| Prompt injection | LLM never executes — it only picks template + params |
| Customer DB exfil | `QUERY_MAX_ROWS` cap, `QUERY_TIMEOUT`, audit log per query |
| LLM data leak | Insight prompt sees only first 5 rows + aggregates |

## Out-of-scope (left as exercise)

- WAF / DDoS — handle at the edge (Cloudflare / ALB)
- 2FA — TOTP would slot into `auth_service.login`
- SAML / OIDC SSO — add a separate identity provider
- Field-level encryption beyond credentials
- Customer-managed keys (CMK / KMS)
- SOC 2 / HIPAA compliance hardening (audit log retention, access reviews)
