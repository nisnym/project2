# 04 · API & Event Contracts (the integration seam)

This is the **single source of truth** that lets 4 members build in parallel. It is
frozen at **IC-0** (see [`00-project-plan.md`](00-project-plan.md#7-integration-checkpoints)).
Any change requires a PR reviewed by every affected service owner.

Conventions:
- All REST is JSON over HTTPS through the gateway at `https://api.local/…` (dev `http://localhost:8080`).
- Every request carries `Authorization: Bearer <access>` unless marked *public*.
- Every request/response carries `X-Trace-Id` (gateway generates if absent).
- Timestamps are ISO-8601 UTC. IDs are UUIDv4. Money is `{ "amount": "100.00", "currency": "USD" }` (string decimal, never float).

---

## 1. Auth & identity propagation

### JWT (RS256) claims — access token
```json
{
  "sub": "b1f2…user-uuid",
  "email": "jane@example.com",
  "roles": ["customer"],
  "type": "access",
  "iat": 1717000000,
  "exp": 1717000900,
  "trace": "…"
}
```
- **access** TTL 15 min, **refresh** TTL 7 days. Refresh rotates; old refresh is blacklisted in Redis.
- Gateway verifies the signature with the **public key** and injects downstream headers:
  `X-User-Id`, `X-User-Roles`, `X-Trace-Id`. Services trust these **only** from the gateway network.

### Endpoints (auth-svc)
| Method | Path | Body | Response |
|--------|------|------|----------|
| POST | `/auth/register` *(public)* | `{email, password, full_name}` | `201 {user_id}` |
| POST | `/auth/login` *(public)* | `{email, password}` | `200 {access, refresh}` |
| POST | `/auth/refresh` *(public)* | `{refresh}` | `200 {access, refresh}` |
| POST | `/auth/logout` | `{refresh}` | `204` (blacklists) |
| GET | `/auth/me` | — | `200 {user_id, email, roles}` |

---

## 2. Standard shapes

### Error envelope (all services)
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "amount must be positive",
    "details": [{"field": "amount", "issue": "min"}],
    "trace_id": "…"
  }
}
```
Common codes: `VALIDATION_ERROR` (400), `UNAUTHENTICATED` (401), `FORBIDDEN` (403),
`NOT_FOUND` (404), `CONFLICT`/`IDEMPOTENCY_REPLAY` (409), `RATE_LIMITED` (429),
`FRAUD_REJECTED` (422), `UPSTREAM_TIMEOUT` (504).

### Pagination (list endpoints)
`GET …?page=1&page_size=20` → `{ "results": [...], "count": N, "next": url|null, "previous": url|null }`

### Idempotency (funding & transfers)
Client sends `Idempotency-Key: <uuid>`. Same key + same body → original response replayed
(`409 IDEMPOTENCY_REPLAY` if body differs). Keys stored in Redis 24h + unique DB index.

---

## 3. REST contracts per service (request → response)

### onboarding-svc (M1)
```
POST /onboarding                      # start / capture
  body: { full_name, dob, address:{...}, phone, national_id }
  201:  { application_id, status:"CAPTURED" }
GET  /onboarding/{id}
  200:  { application_id, status, kyc:{status,score}, eligibility:{decision,tier},
          account:{account_number,ifsc,swift,status}|null }
```

### kyc-svc (M2)
```
POST /kyc/{application_id}/documents  # multipart or presigned-put callback
  body: { doc_type:"ID|ADDRESS|SELFIE", object_key }
  202:  { kyc_case_id, status:"PENDING" }
GET  /kyc/{application_id}
  200:  { kyc_case_id, status, score, checks:{identity_match, document_valid,
          sanctions_hit, pep_hit} }
```

### eligibility-svc (M2)
```
GET  /eligibility/{application_id}
  200:  { decision:"PASS|FAIL|REVIEW", risk_score, product_tier,
          factors:[{code,weight,description}] }
```

### account-svc (M2)
```
GET  /accounts/me                     # accounts for the JWT user
  200:  { results:[{ account_id, account_number, ifsc, swift, currency,
          status, balance }] }
GET  /accounts/{id}
  200:  { account_id, account_number, ifsc, swift, currency, status, balance }
```

### funding-svc (M3)
```
POST /funding                         # Idempotency-Key required
  body: { account_id, amount:{amount,currency}, source:"CARD|BANK|UPI" }
  202:  { funding_id, status:"PENDING" }        # async → funding.completed
GET  /funding/{id}
  200:  { funding_id, status:"PENDING|COMPLETED|REJECTED", amount, source }
```

### transfer-svc (M3)
```
POST /transfers                       # Idempotency-Key required
  body: { from_account_id, to_account_ref, amount:{amount,currency},
          rail:"DOMESTIC|INTERNATIONAL", note? }
  202:  { transfer_id, status:"INITIATED" }
GET  /transfers/{id}
  200:  { transfer_id, status:"INITIATED|APPROVED|REJECTED|HELD|SETTLED",
          amount, rail, fraud:{decision,score}|null }
GET  /transfers?account_id=&page=     # history
```

### fraud-svc (M4) — **internal, called by payment-svc**
```
POST /fraud/screen                    # sync, 50ms budget, service-to-service auth
  body: { payment_id, account_id, amount:{amount,currency}, rail,
          to_account_ref, device_hash, geo, kind:"FUNDING|TRANSFER" }
  200:  { decision:"APPROVE|REJECT|REVIEW", score, latency_ms,
          signals:[{code,contribution,detail}], model_version }
GET  /fraud/decisions/{payment_id}    # ops/audit read
```

### ops-svc (M4)
```
GET   /ops/cases?status=&page=        # ops_analyst
  200:  { results:[{ case_id, payment_id, score, status, created_at }], count }
PATCH /ops/cases/{id}
  body: { status:"CONFIRMED|FALSE_POSITIVE|CLOSED", resolution }
  200:  { case_id, status }           # FALSE_POSITIVE → feeds rule-weight tuning
GET   /ops/metrics                    # transaction health, fp-rate, latency p99
```

### audit-svc (M4)
```
GET  /audit?trace_id=                 # compliance_officer; full chain for a flow
GET  /audit?actor_id=&from=&to=
  200:  { results:[{ id, event_type, producer, actor_id, occurred_at,
          hash, prev_hash }], count }
```

### notification-svc (M2)
```
GET  /notifications/me
  200:  { results:[{ id, channel, template, status, created_at }] }
# inbound is event-driven (no public POST); WebSocket /ws/notifications for live push
```

---

## 4. Event backbone (Kafka)

### 4.1 Envelope (every message on every topic)
```json
{
  "event_id": "uuid",
  "event_type": "account.opened",
  "occurred_at": "2026-08-05T10:00:00Z",
  "producer": "account-svc",
  "trace_id": "uuid",
  "version": 1,
  "data": { }
}
```
- Topic name == `event_type`. Partition key == `account_id` (or `user_id` pre-account) to preserve per-entity ordering.
- Producers use the **transactional outbox**; consumers are **idempotent** (dedupe on `event_id`).
- `version` allows additive, backward-compatible schema evolution.

### 4.2 Topic catalogue & payloads (`data`)

| Topic (`event_type`) | Producer | Key consumers | `data` payload |
|----------------------|----------|---------------|----------------|
| `user.registered` | auth | audit | `{user_id, email}` |
| `customer.captured` | onboarding | kyc, audit | `{application_id, user_id, customer_info}` |
| `kyc.requested` | kyc | audit | `{kyc_case_id, application_id}` |
| `kyc.completed` | kyc | onboarding, eligibility, audit | `{application_id, status, score, checks}` |
| `eligibility.evaluated` | eligibility | onboarding, account, audit | `{application_id, decision, risk_score, product_tier}` |
| `account.opened` | account | onboarding, funding, notification, audit | `{account_id, user_id, account_number, ifsc, swift, currency}` |
| `funding.requested` | funding | payment, fraud, audit | `{funding_id, account_id, amount, source}` |
| `funding.completed` | funding | notification, audit | `{funding_id, account_id, status}` |
| `transfer.initiated` | transfer | payment, fraud, audit | `{transfer_id, from_account_id, to_account_ref, amount, rail}` |
| `transfer.settled` | transfer | notification, audit | `{transfer_id, status}` |
| `payment.processed` | payment | funding, transfer, notification, audit | `{payment_id, kind, source_ref, status, amount}` |
| `payment.failed` | payment | funding, transfer, notification, ops, audit | `{payment_id, source_ref, reason}` |
| `fraud.completed` | fraud | audit, (training) | `{payment_id, decision, score, signals, latency_ms}` |
| `fraud.flagged` | fraud | ops, notification, audit | `{payment_id, account_id, score, signals}` |
| `transaction.approved` | payment | notification, ops, audit | `{payment_id, account_id, amount}` |
| `transaction.rejected` | payment | notification, ops, audit | `{payment_id, account_id, reason}` |
| `notification.sent` | notification | audit | `{notification_id, user_id, channel, status}` |
| `case.updated` | ops | fraud (rule tuning), audit | `{case_id, payment_id, status, resolution}` |

### 4.3 Consumer contract
- **At-least-once** delivery → all consumers dedupe on `event_id` (unique index / Redis set).
- Handlers are **pure functions of the event** (no hidden cross-service reads on the hot path where avoidable).
- Poison messages → dead-letter topic `<event_type>.dlq` after N retries; audit-svc still records the attempt.

---

## 5. Contract testing (how we keep the seam honest)
- Each producer publishes a **sample event** fixture per topic in `libs/common_events/fixtures/`.
- Each consumer has a **contract test** that asserts it handles the current fixture (Pact-style).
- Each REST service exposes `/api/schema` (OpenAPI); the React app generates typed clients from it, and `schemathesis` fuzzes it in CI.
- Breaking a fixture or schema **fails CI** and pings the owning members.
