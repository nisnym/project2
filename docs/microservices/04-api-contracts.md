# 04 · API Contracts

> The synchronous surface. Public routes go through the edge; `/internal/*`
> routes are service-to-service only and are never exposed publicly.
> Every service publishes its own OpenAPI 3 schema via `drf-spectacular` at
> `/api/schema` with Swagger UI at `/api/docs` — **those are the contract**;
> this document is the readable summary and the shared conventions.

> ⚠ **This document is the designed surface, and it is wider than the built
> one.** Paths have been corrected to what exists, but several endpoints listed
> here were never implemented — notably `/api/accounts/{id}/statement`,
> `/api/beneficiaries/{id}/verify`, `/api/accounts/{id}/freeze`,
> `/api/funding-sources/{id}/verify`, `/api/transactions/{id}/retry` and
> `/api/ops/dlq/*`. For the exact built surface, run a service and read
> `/api/docs`, or see [`../HANDBOOK.md`](../HANDBOOK.md); `verify_spa_api.py`
> exercises every endpoint the SPA actually calls.

---

## 1. Conventions

### 1.1 Routing

| Prefix | Reachable from | Auth |
|---|---|---|
| `/api/**` | Internet, via gateway | User JWT (RS256, Bearer) |
| `/internal/**` | Private network only | Service JWT + HMAC body signature |
| `/healthz`, `/readyz` | Cluster only | None |
| `/api/schema`, `/api/docs` | Internet (non-prod) / private (prod) | None / admin |

Gateway path map:

```
/api/auth/*            → identity-svc        /api/transfers/*       → payments-svc
/api/onboarding/*      → onboarding-svc      /api/schedules/* → payments-svc
/api/kyc/*             → kyc-svc             /api/funding/*         → payments-svc
/api/accounts/*        → account-svc         /api/fraud/*           → fraud-svc
/api/beneficiaries/*   → account-svc         /api/notifications/*   → notification-svc
/api/audit/*           → audit-svc           /api/ops/*             → ops-svc
                    ledger-svc: NO PUBLIC ROUTE (internal only, by design)
```

### 1.2 Standard headers

| Header | Direction | Notes |
|---|---|---|
| `Authorization: Bearer <jwt>` | in | RS256, verified against the cached JWKS |
| `Idempotency-Key: <uuid4>` | in | **Required** on every money-mutating POST |
| `X-Correlation-Id` | in/out | Generated at the gateway if absent; echoed on every response and propagated to every downstream call and event |
| `X-Request-Id` | out | Per-hop id, distinct from the correlation id |
| `X-Signature` | internal | `HMAC-SHA256(body, pair_key)` on `/internal/*` |

### 1.3 Error envelope

Every non-2xx response, from every service, has this shape. No exceptions.

```json
{
  "error": {
    "code": "INSUFFICIENT_FUNDS",
    "message": "Available balance is lower than the transfer amount.",
    "detail": { "available": "1200.0000", "requested": "5000.0000", "currency": "INR" },
    "field_errors": { "amount": ["Exceeds available balance."] },
    "correlation_id": "c0ffee00-1111-2222-3333-444455556666",
    "retryable": false,
    "documentation_url": "/api/docs#tag/errors/INSUFFICIENT_FUNDS"
  }
}
```

`retryable` is a machine-readable contract: `true` means the same request with the
same `Idempotency-Key` may safely be sent again.

**Canonical error codes**

| HTTP | Code | Retryable | Meaning |
|---|---|---|---|
| 400 | `VALIDATION_ERROR` | ✗ | `field_errors` is populated |
| 400 | `IDEMPOTENCY_KEY_REQUIRED` | ✗ | Money-mutating POST without a key |
| 401 | `TOKEN_INVALID` / `TOKEN_EXPIRED` | ✗ | Refresh and retry |
| 403 | `FORBIDDEN_ROLE` / `NOT_ACCOUNT_OWNER` | ✗ | Role vs ownership — distinct failures |
| 404 | `NOT_FOUND` | ✗ | Also returned instead of 403 where existence itself is sensitive |
| 409 | `IDEMPOTENCY_KEY_CONFLICT` | ✗ | Same key, different body |
| 409 | `REQUEST_IN_PROGRESS` | ✓ | Same key still executing; honour `Retry-After` |
| 409 | `ILLEGAL_STATE_TRANSITION` | ✗ | e.g. cancelling a `DISPATCHED` transfer |
| 422 | `INSUFFICIENT_FUNDS` | ✗ | |
| 422 | `LIMIT_EXCEEDED` | ✗ | `detail` names which limit |
| 422 | `BENEFICIARY_COOLING_OFF` | ✗ | `detail.available_at` |
| 422 | `TRANSACTION_BLOCKED` | ✗ | Fraud `BLOCK` — deliberately vague to the customer |
| 429 | `RATE_LIMITED` | ✓ | `Retry-After` |
| 503 | `SERVICE_UNAVAILABLE` | ✓ | Downstream down or circuit open |

> **Fraud responses never leak rule detail to customers.** A blocked transfer
> returns `TRANSACTION_BLOCKED` with a generic message; reason codes appear only
> on analyst and admin endpoints. Telling an attacker which rule fired is telling
> them how to evade it.

### 1.4 Pagination

Cursor-based everywhere — offset pagination drifts when rows are inserted between
pages, which for a transaction history is a correctness bug, not a UX quirk.

```
GET /api/transfers?limit=25&cursor=eyJjcmVhdGVkX2F0IjoiMjAyNi0wOC0wN1QxMTo0MiIsImlkIjoiLi4uIn0

{ "results": [...], "next_cursor": "eyJ...", "has_more": true }
```

### 1.5 Money in JSON

```json
{ "amount": "150000.0000", "currency": "INR" }
```

Always a **string**, always 4 decimal places, always with an explicit currency.
Any endpoint accepting a bare number for money is a defect.

---

## 2. identity-svc

| Method | Path | Role | Notes |
|---|---|---|---|
| POST | `/api/auth/register` | — | Creates a `PENDING` user; onboarding activates it |
| POST | `/api/auth/login` | — | Returns tokens, or `mfa_required` |
| POST | `/api/auth/mfa/verify` | — | TOTP; completes login |
| POST | `/api/auth/refresh` | — | Rotates; reuse revokes the whole family |
| POST | `/api/auth/logout` | any | Revokes the presented refresh token |
| GET | `/api/auth/me` | any | Profile + role + linked accounts |
| GET | `/.well-known/jwks.json` | — | Public keys, cached 5 min by every service |
| POST | `/internal/token` | service | `client_credentials` → service JWT |

```jsonc
// POST /api/auth/login
{ "email": "asha@example.com", "password": "...", "device_fingerprint": "sha256:..." }
// 200
{ "access_token": "eyJ...", "token_type": "Bearer", "expires_in": 900,
  "refresh_token": "eyJ...", "user": {"id": "...", "email": "...", "role": "CUSTOMER"},
  "mfa_required": false }
```

**Access-token claims:** `sub`, `role`, `email`, `iat`, `exp`, `jti`, `iss`,
`aud`, `device_id`. **Service-token claims:** `sub` (service name), `scope`
(e.g. `["ledger:write","fraud:screen"]`), short `exp`.

---

## 3. onboarding-svc & kyc-svc

| Method | Path | Role |
|---|---|---|
| POST | `/api/onboarding/applications` | CUSTOMER |
| GET | `/api/onboarding/applications/{id}` | owner |
| PATCH | `/api/onboarding/applications/{id}` | owner (only in `DRAFT`) |
| POST | `/api/onboarding/applications/{id}/submit` | owner |
| POST | `/api/kyc/cases/{id}/documents` | owner — multipart |
| GET | `/api/kyc/cases/{id}` | owner — status only, never raw PII |
| POST | `/api/onboarding/applications/{id}/decision` | OPS — manual-review override |

```jsonc
// POST /api/onboarding/applications   → 201
{ "customer_info": {
    "full_name": "Asha Menon", "date_of_birth": "1994-03-12", "nationality": "IN",
    "address": {"line1": "...", "city": "Kochi", "postal_code": "682001", "country": "IN"},
    "employment_status": "SALARIED", "annual_income": {"amount": "1200000.00", "currency": "INR"},
    "purpose": "SALARY_ACCOUNT" },
  "consents": {"terms": true, "data_processing": true} }

// GET /api/onboarding/applications/{id}  — the SPA polls this
{ "id": "...", "status": "ACCOUNT_OPENED",
  "timeline": [ {"status": "SUBMITTED", "at": "..."}, {"status": "KYC_PENDING", "at": "..."},
                {"status": "KYC_PASSED", "at": "..."}, {"status": "ACCOUNT_OPENED", "at": "..."} ],
  "kyc": {"status": "PASSED", "risk_rating": "LOW"},
  "eligibility": {"decision": "PASS", "risk_score": 22, "tier": "STANDARD",
                  "factors": [{"code": "INCOME_BAND_3", "points": 20}]},
  "account": {"id": "...", "account_number": "5021-8834-7731", "ifsc": "INGB0000521",
              "currency": "INR", "limits": {"per_txn_max": "200000.0000", "daily_max": "500000.0000"}} }
```

---

## 4. account-svc

| Method | Path | Role |
|---|---|---|
| GET | `/api/accounts` | CUSTOMER — own accounts, cached balances |
| GET | `/api/accounts/{id}` | owner |
| GET | `/api/accounts/{id}` (balance embedded) | owner — proxies the ledger, may be `stale` |
| GET | `/api/accounts/{id}/statement?from=&to=` | owner — async for large ranges |
| GET/POST | `/api/beneficiaries` | owner |
| GET/PATCH/DELETE | `/api/beneficiaries/{id}` | owner |
| POST | `/api/beneficiaries/{id}/verify` | owner — penny-drop, simulated |
| GET/POST/PATCH | `/api/limit-policies` | **ADMIN** |
| POST | `/api/accounts/{id}/freeze` \| `/unfreeze` | OPS |
| POST | `/internal/validate-transfer` | service (payments-svc) |
| POST | `/internal/limits/release` | service |
| POST | `/internal/accounts` | service (onboarding-svc) |
| GET | `/internal/balances/{account_id}` | service |

```jsonc
// GET /api/accounts/{id}
{ "account_id": "...", "available": {"amount": "348200.0000", "currency": "INR"},
  "ledger_balance": {"amount": "498200.0000", "currency": "INR"},
  "held": {"amount": "150000.0000", "currency": "INR"},
  "as_of": "2026-08-07T11:42:19Z", "source": "LEDGER", "stale": false }

// POST /internal/validate-transfer  (called synchronously inside the saga)
{ "account_id": "...", "user_id": "...", "beneficiary_id": "...",
  "amount": {"amount": "150000.0000", "currency": "INR"}, "rail": "INTERNATIONAL" }
// 200
{ "ok": true, "reservation_id": "...", "account_status": "ACTIVE",
  "beneficiary": {"type": "INTERNATIONAL", "country": "AE", "currency": "AED",
                  "fingerprint": "sha256:...", "age_hours": 3.2, "in_cooling_off": true},
  "limits": {"per_txn_max": "200000.0000", "daily_remaining": "350000.0000"} }
// 422
{ "error": {"code": "LIMIT_EXCEEDED", "detail": {"limit": "DAILY_MAX", "cap": "500000.0000",
            "used": "420000.0000", "requested": "150000.0000"}, "retryable": false} }
```

```jsonc
// POST /api/beneficiaries   → 201  (ADMIN-visible cooling-off is an anti-fraud control)
{ "nickname": "Ravi — Dubai", "beneficiary_type": "INTERNATIONAL",
  "account_number": "AE070331234567890123456", "swift_bic": "EBILAEAD",
  "country": "AE", "currency": "AED" }
// 201
{ "id": "...", "status": "PENDING", "cooling_off_until": "2026-08-08T11:00:00Z",
  "message": "Transfers above ₹10,000 to this payee are restricted for 24 hours." }
```

---

## 5. payments-svc

| Method | Path | Role | Idempotency-Key |
|---|---|---|---|
| POST | `/api/funding` | CUSTOMER | **required** |
| GET | `/api/funding/{id}` | owner | |
| GET/POST | `/api/funding-sources` | owner | |
| POST | `/api/funding-sources/{id}/verify` | owner | |
| POST | `/api/transfers` | CUSTOMER | **required** |
| GET | `/api/transfers` | owner — cursor paginated | |
| GET | `/api/transactions/{id}` | owner — full status timeline | |
| POST | `/api/transactions/{id}/cancel` | owner | |
| GET/POST | `/api/schedules` | owner | **required** on POST |
| PATCH/DELETE | `/api/schedules/{id}` | owner — pause/resume/cancel | |
| POST | `/api/transactions/{id}/retry` | OPS | **required** |
| GET | `/internal/transactions/{id}` | service | |

```jsonc
// POST /api/transfers    Idempotency-Key: 3f1c...    → 201
{ "account_id": "...", "beneficiary_id": "...",
  "amount": {"amount": "150000.00", "currency": "INR"},
  "rail": "INTERNATIONAL", "purpose_code": "FAMILY_MAINTENANCE",
  "remarks": "August", "execute_at": null,
  "context": {"device_fingerprint": "sha256:...", "ip_country": "IN"} }

// 201 — ALLOW
{ "id": "...", "reference": "TXN-20260807-A7F3K2", "status": "DISPATCHED",
  "amount": {"amount": "150000.0000", "currency": "INR"},
  "dest_amount": {"amount": "6420.5500", "currency": "AED"}, "fx_rate": "0.042803",
  "rail": "INTERNATIONAL", "beneficiary": {"id": "...", "nickname": "Ravi — Dubai", "masked": "••••3456"},
  "fraud": {"decision": "ALLOW", "score": 22},
  "estimated_settlement": "2026-08-09T12:00:00Z", "created_at": "..." }

// 201 — REVIEW (funds held; NOT an error, so still 201)
{ "id": "...", "reference": "...", "status": "UNDER_REVIEW",
  "fraud": {"decision": "REVIEW", "score": 58},
  "message": "This transfer is being reviewed for your security. Funds are on hold.",
  "expected_resolution_by": "2026-08-07T13:42:18Z" }

// 422 — BLOCK (deliberately uninformative)
{ "error": {"code": "TRANSACTION_BLOCKED",
            "message": "We couldn't complete this transfer. Please contact support.",
            "detail": {"reference": "TXN-20260807-A7F3K2"}, "retryable": false} }
```

```jsonc
// GET /api/transactions/{id}   — what the tracking screen polls
{ "id": "...", "reference": "TXN-20260807-A7F3K2", "status": "DISPATCHED",
  "timeline": [
    {"status": "INITIATED",  "at": "11:42:18.102", "detail": null},
    {"status": "VALIDATED",  "at": "11:42:18.147", "detail": "Limits OK"},
    {"status": "RESERVED",   "at": "11:42:18.201", "detail": "Funds held"},
    {"status": "SCREENING",  "at": "11:42:18.208", "detail": null},
    {"status": "APPROVED",   "at": "11:42:18.220", "detail": "Fraud score 22 · 12 ms"},
    {"status": "POSTED",     "at": "11:42:18.286", "detail": "JE-000184213"},
    {"status": "DISPATCHED", "at": "11:42:19.004", "detail": "SWIFT-99213"}
  ],
  "cancellable": false,
  "cancellation_reason": "Transfer has already been sent to the payment network." }
```

```jsonc
// POST /api/schedules   → 201   (recurring transfer use case)
{ "account_id": "...", "beneficiary_id": "...",
  "amount": {"amount": "25000.00", "currency": "INR"}, "rail": "DOMESTIC",
  "frequency": "MONTHLY", "start_date": "2026-09-01", "end_date": "2027-08-31",
  "max_runs": 12 }
// 201
{ "id": "...", "status": "ACTIVE", "next_run_at": "2026-09-01T09:00:00Z",
  "runs_completed": 0, "max_runs": 12 }
```

```jsonc
// POST /api/transactions/{id}/cancel
// 200
{ "id": "...", "status": "CANCELLED", "hold_released": true, "refunded": false }
// 409 — cancellation is state-dependent, and the reason is explicit
{ "error": {"code": "ILLEGAL_STATE_TRANSITION",
            "message": "This transfer can no longer be cancelled.",
            "detail": {"current_status": "DISPATCHED",
                       "cancellable_from": ["INITIATED","VALIDATED","RESERVED","UNDER_REVIEW","SCHEDULED"]},
            "retryable": false} }
```

```jsonc
// POST /api/funding    Idempotency-Key required
{ "account_id": "...", "funding_source_id": "...",
  "amount": {"amount": "50000.00", "currency": "INR"} }
// 201
{ "id": "...", "reference": "FND-20260807-K92XQ1", "status": "SETTLED",
  "source": {"type": "EXTERNAL_BANK", "display_name": "HDFC ••••4821"},
  "amount": {"amount": "50000.0000", "currency": "INR"},
  "new_balance": {"amount": "398200.0000", "currency": "INR"},
  "fraud": {"decision": "ALLOW", "score": 8} }
```

---

## 6. ledger-svc — internal only

| Method | Path | Scope |
|---|---|---|
| POST | `/internal/holds` | `ledger:write` |
| POST | `/internal/holds/{id}/capture` | `ledger:write` |
| POST | `/internal/holds/{id}/release` | `ledger:write` |
| POST | `/internal/journal-entries` | `ledger:write` |
| POST | `/internal/journal-entries/{id}/reverse` | `ledger:write` |
| GET | `/internal/balances/{account_ref}` | `ledger:read` |
| GET | `/internal/accounts/{ref}/postings` | `ledger:read` |
| POST | `/internal/ledger-accounts` | `ledger:write` |
| GET | `/internal/trial-balance?date=` | `ledger:read` |

```jsonc
// POST /internal/holds      Idempotency-Key: <transaction_id>
{ "account_ref": "...", "amount": {"amount": "150000.0000", "currency": "INR"},
  "txn_ref": "...", "ttl_minutes": 30 }
// 201
{ "hold_id": "...", "status": "ACTIVE", "expires_at": "2026-08-07T12:12:18Z",
  "available_after": {"amount": "348200.0000", "currency": "INR"} }
// 422
{ "error": {"code": "INSUFFICIENT_FUNDS",
            "detail": {"available": "120000.0000", "requested": "150000.0000"}, "retryable": false} }

// POST /internal/holds/{id}/capture    Idempotency-Key: capture:<transaction_id>
{ "entry_type": "TRANSFER", "narrative": "Transfer to ••••3456",
  "legs": [
    {"ledger_account_code": "CUST:<uuid>",              "direction": "DEBIT",  "amount": "150000.0000", "currency": "INR"},
    {"ledger_account_code": "INTERNAL:CLEARING_INTL",   "direction": "CREDIT", "amount": "150000.0000", "currency": "INR"}
  ] }
// 201
{ "journal_entry_id": "...", "reference": "JE-000184213", "posted_at": "...",
  "postings": [ {"...": "...", "balance_after": "348200.0000"}, {"...": "..."} ] }
```

**Every write to ledger-svc requires an `Idempotency-Key`, and the key is
derived deterministically from the transaction id** (`<txn_id>`,
`capture:<txn_id>`, `rev:<txn_id>`). That way a payments-svc retry — from any
cause, including a lost response — can never double-post.

---

## 7. fraud-svc

| Method | Path | Role |
|---|---|---|
| POST | `/internal/screen` | service (`fraud:screen`) — the 50 ms path |
| GET | `/api/fraud/cases` | FRAUD_ANALYST — filter by status/priority/SLA |
| GET | `/api/fraud/cases/{id}` | FRAUD_ANALYST — full feature vector + reason codes |
| POST | `/api/fraud/cases/{id}/assign` | FRAUD_ANALYST |
| POST | `/api/fraud/cases/{id}/approve` | FRAUD_ANALYST |
| POST | `/api/fraud/cases/{id}/reject` | FRAUD_ANALYST |
| GET | `/api/fraud/decisions/{id}` | FRAUD_ANALYST |
| GET/POST | `/api/fraud/rules` | **ADMIN** |
| GET/PATCH/DELETE | `/api/fraud/rules/{id}` | **ADMIN** |
| POST | `/api/fraud/rules/{id}/test` | ADMIN — dry-run against historical decisions |
| GET/PATCH | `/api/fraud/thresholds` | **ADMIN** |
| GET/POST/DELETE | `/api/fraud/lists` | ADMIN — blacklists, high-risk countries |
| GET | `/api/fraud/stats` | ADMIN — precision, FP rate, latency percentiles |

```jsonc
// POST /internal/screen     ⏱ p99 < 50 ms
{ "txn_ref": "...", "account_ref": "...", "user_ref": "...",
  "amount": {"amount": "150000.0000", "currency": "INR"},
  "rail": "INTERNATIONAL", "txn_type": "TRANSFER",
  "beneficiary": {"fingerprint": "sha256:...", "country": "AE",
                  "age_hours": 3.2, "in_cooling_off": true},
  "context": {"device_fingerprint": "sha256:...", "ip_country": "IN",
              "session_age_s": 240, "channel": "WEB"} }
// 200
{ "decision_id": "...", "decision": "REVIEW", "score": 58,
  "reason_codes": ["R005", "R015"], "latency_ms": 12,
  "case_id": "...", "ruleset_version": "2026-08-07.3" }
```

```jsonc
// GET /api/fraud/cases/{id}   — everything an analyst needs on one screen
{ "id": "...", "status": "OPEN", "priority": "HIGH", "sla_due_at": "...",
  "transaction": {"reference": "TXN-20260807-A7F3K2", "amount": {...}, "rail": "INTERNATIONAL",
                  "beneficiary_masked": "••••3456", "status": "UNDER_REVIEW"},
  "decision": {"score": 58, "latency_ms": 12, "ruleset_version": "2026-08-07.3",
    "reason_codes": [
      {"code": "R005", "rule": "New beneficiary + large amount", "weight": 45,
       "precision": 0.34, "fired_count": 1284},
      {"code": "R015", "rule": "Beneficiary in cooling-off", "weight": 13,
       "precision": 0.61, "fired_count": 402}],
    "shadow_codes": ["R017"] },
  "features": {"amount_zscore": 3.8, "txn_count_5m": 1, "beneficiary_is_new": true,
               "beneficiary_age_hours": 3.2, "country_changed": false, "device_is_new": false,
               "distinct_benef_24h": 2, "account_age_days": 412},
  "account_context": {"account_age_days": 412, "prior_cases": 0, "avg_txn": "18400.0000"},
  "audit_trail_url": "/api/audit/logs?correlation_id=c0ffee00-..." }
```

```jsonc
// POST /api/fraud/cases/{id}/approve
{ "resolution": "FALSE_POSITIVE", "note": "Called customer; payee confirmed." }
// 200
{ "id": "...", "status": "APPROVED", "transaction_status": "PROCESSING",
  "message": "Transfer released. Rule statistics updated." }

// POST /api/fraud/rules   → 201  (ADMIN; new rules default to SHADOW)
{ "code": "R018_RAPID_FANOUT", "name": "Rapid fan-out to new payees",
  "condition": {"all": [{"fact": "distinct_benef_5m", "op": "gte", "value": 3},
                        {"fact": "beneficiary_is_new", "op": "eq", "value": true}]},
  "weight": 55, "hard_block": false, "reason_code": "R018",
  "category": "VELOCITY", "mode": "SHADOW" }
// 400 — the DSL is validated at write time, never at screen time
{ "error": {"code": "VALIDATION_ERROR",
            "field_errors": {"condition": ["Unknown fact 'benef_count_5min'. Valid facts: amount, rail, ..."]}} }

// POST /api/fraud/rules/{id}/test   — replay against stored feature vectors
{ "sample_days": 7 }
// 200
{ "evaluated": 48213, "would_fire": 312, "fire_rate": 0.0065,
  "overlap_with_confirmed_fraud": 41, "overlap_with_false_positives": 12,
  "estimated_precision": 0.77,
  "decision_shifts": {"ALLOW→REVIEW": 287, "REVIEW→BLOCK": 25} }
```

> `POST /rules/{id}/test` is possible only because `FraudDecision.features`
> stores the exact feature vector for every past screening. Storing it costs a
> JSONB column and buys risk-free rule tuning — the highest-leverage 20 bytes in
> the schema.

---

## 8. notification-svc, audit-svc, ops-svc

| Method | Path | Role |
|---|---|---|
| GET | `/api/notifications?since=<cursor>&unread=true` | owner — polled by the SPA |
| POST | `/api/notifications/{id}/read` \| `/read-all` | owner |
| GET/PATCH | `/api/notifications/preferences` | owner |
| GET | `/api/audit/logs?correlation_id=&actor_id=&aggregate_id=&from=&to=` | OPS / ADMIN |
| GET | `/api/audit/logs/{id}/verify` | ADMIN — verify this row's chain link |
| POST | `/api/audit/exports` | ADMIN — async job → downloadable artefact |
| GET | `/api/ops/queues` | OPS — every service's queue depth and lag |
| GET | `/api/ops/health` | OPS — latest `HealthSnapshot` per service |
| GET | `/api/ops/failures?status=OPEN` | OPS |
| POST | `/api/ops/failures/{id}/retry` \| `/resolve` | OPS |
| GET | `/api/ops/dlq` | OPS — dead outbox/inbox rows across services |
| POST | `/api/ops/dlq/{id}/replay` | OPS |
| POST | `/api/ops/reports` | ADMIN — async |
| GET | `/api/ops/reports/{id}` | ADMIN — status + download URL |

```jsonc
// GET /api/ops/queues     — the "monitor transfer queues" use case
{ "captured_at": "2026-08-07T11:45:00Z",
  "services": [
    {"service": "payments-svc", "reachable": true, "queue_depth": 12,
     "oldest_pending_age_s": 3, "failed_tasks_24h": 2, "outbox_pending": 4, "outbox_dead": 0,
     "status": "HEALTHY"},
    {"service": "notification-svc", "reachable": true, "queue_depth": 341,
     "oldest_pending_age_s": 187, "failed_tasks_24h": 47, "outbox_pending": 0, "outbox_dead": 3,
     "status": "DEGRADED", "reason": "queue depth above threshold; 3 dead events"},
    {"service": "fraud-svc", "reachable": false, "status": "UNREACHABLE",
     "last_seen": "2026-08-07T11:41:30Z"} ],
  "transfer_pipeline": {"under_review": 8, "compensation_pending": 1,
                        "dispatched_awaiting_settlement": 34, "failed_24h": 3} }

// GET /api/ops/failures?status=OPEN     — "investigate failed transfers"
{ "results": [
  { "id": "...", "failure_type": "PAYMENT_RETURNED", "source_service": "payments-svc",
    "subject_ref": "...", "correlation_id": "c0ffee00-...",
    "detail": {"reference": "TXN-20260806-Q41M8", "rail": "INTERNATIONAL",
               "return_reason": "BENEFICIARY_ACCOUNT_CLOSED",
               "amount": {"amount": "85000.0000", "currency": "INR"},
               "funds_returned": true, "reversal_journal_entry_id": "..."},
    "status": "OPEN", "opened_at": "...",
    "audit_trail_url": "/api/audit/logs?correlation_id=c0ffee00-...",
    "actions": ["RETRY", "RESOLVE", "CONTACT_CUSTOMER"] } ],
  "next_cursor": null }
```

```jsonc
// POST /api/ops/reports   → 202     — "generate system reports"
{ "report_type": "FRAUD_SUMMARY", "params": {"from": "2026-08-01", "to": "2026-08-07"} }
// 202
{ "id": "...", "status": "QUEUED", "poll_url": "/api/ops/reports/..." }
// GET later
{ "id": "...", "status": "READY", "rows": 1842,
  "download_url": "/api/ops/reports/.../download", "expires_at": "...",
  "summary": {"screened": 48213, "allowed": 46901, "reviewed": 1198, "blocked": 114,
              "false_positive_rate": 0.41, "p99_latency_ms": 19,
              "top_rules": [{"code": "R005", "fired": 412, "precision": 0.34}]} }
```

---

## 9. Authorisation matrix

Role alone is never sufficient — **ownership is checked separately** on every
customer-scoped resource.

| Endpoint group | CUSTOMER | FRAUD_ANALYST | OPS | ADMIN |
|---|:--:|:--:|:--:|:--:|
| `/api/auth/*` (own) | ✅ | ✅ | ✅ | ✅ |
| `/api/onboarding/*` | own | — | read | read |
| `/api/accounts/*` (own) | ✅ | — | read | read |
| `/api/accounts/{id}/freeze` | — | — | ✅ | ✅ |
| `/api/limit-policies` | — | — | read | ✅ |
| `/api/beneficiaries/*` (own) | ✅ | — | read | read |
| `/api/funding/*`, `/api/transfers/*` (own) | ✅ | — | read | read |
| `/api/transactions/{id}/retry` | — | — | ✅ | ✅ |
| `/api/fraud/cases/*` | — | ✅ | read | ✅ |
| `/api/fraud/rules`, `/thresholds`, `/lists` | — | read | read | ✅ |
| `/api/notifications/*` (own) | ✅ | ✅ | ✅ | ✅ |
| `/api/audit/*` | — | read (own cases) | ✅ | ✅ |
| `/api/ops/*` | — | — | ✅ | ✅ |
| `/internal/*` | — | — | — | — (service tokens only) |

```python
# platform_common/auth/permissions.py
class IsAccountOwner(BasePermission):
    """Role says WHAT you may do; this says TO WHOSE DATA. Both are always required."""
    def has_object_permission(self, request, view, obj):
        if request.user.role in ("OPS", "ADMIN"):
            return request.method in SAFE_METHODS          # staff read, never write, customer data
        return str(obj.user_id) == str(request.user.id)
```

---

## 10. Contract testing

1. Every service commits its generated `openapi.json` (`manage.py spectacular`).
2. CI regenerates it and fails on an undeclared diff — schema drift cannot merge silently.
3. Consumers keep a **consumer-expectations** file naming the fields they read; a provider test asserts each is still present and still the same type.
4. Breaking changes require a version bump and a deprecation window; `X-API-Deprecation` announces the sunset date.
5. The SPA's Zod schemas are generated from the same OpenAPI files, so a drift becomes a caught frontend error rather than a blank screen.

```python
# services/ledger/tests/test_consumer_contract.py
CONSUMER_EXPECTATIONS = {
    "payments-svc": {
        "POST /internal/holds":            ["hold_id", "status", "expires_at"],
        "POST /internal/holds/{id}/capture": ["journal_entry_id", "reference", "posted_at"],
    }
}
# asserts every listed field is present, correctly typed, and non-null in a live response
```
