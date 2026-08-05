# 02 · Low-Level Design (LLD)

Per-service data models, state machines, sequences, and the internals of the
tricky bits (fraud scoring, ledger, idempotency). REST/event contracts are in
[`04-api-contracts.md`](04-api-contracts.md). Editable versions: the **"LLD"**
pages inside the two `.drawio` files.

---

## 1. Data models (ER per service — DB-per-service)

### 1.1 UC1 — Onboarding domain

```mermaid
erDiagram
  USERS ||--o{ ONBOARDING_APPLICATIONS : "starts"
  ONBOARDING_APPLICATIONS ||--o{ APPLICATION_EVENTS : "logs"
  ONBOARDING_APPLICATIONS ||--|| KYC_CASES : "triggers"
  KYC_CASES ||--o{ KYC_DOCUMENTS : "has"
  KYC_CASES ||--|| KYC_CHECKS : "produces"
  ONBOARDING_APPLICATIONS ||--|| ELIGIBILITY_ASSESSMENTS : "scored by"
  ELIGIBILITY_ASSESSMENTS ||--o{ RISK_FACTORS : "explained by"
  ONBOARDING_APPLICATIONS ||--|| ACCOUNTS : "results in"

  USERS {
    uuid id PK
    string email UK
    string password_hash
    string role
    bool is_active
    timestamp created_at
  }
  ONBOARDING_APPLICATIONS {
    uuid id PK
    uuid user_id FK
    string status "state machine"
    jsonb customer_info "name,dob,address,phone,pan"
    uuid trace_id
    timestamp created_at
    timestamp updated_at
  }
  APPLICATION_EVENTS {
    uuid id PK
    uuid application_id FK
    string from_state
    string to_state
    jsonb meta
    timestamp created_at
  }
  KYC_CASES {
    uuid id PK
    uuid application_id FK
    string status "PENDING/PASSED/FAILED/REVIEW"
    int score
    timestamp created_at
  }
  KYC_DOCUMENTS {
    uuid id PK
    uuid kyc_case_id FK
    string doc_type "ID/ADDRESS/SELFIE"
    string object_key "MinIO"
    string status
  }
  KYC_CHECKS {
    uuid id PK
    uuid kyc_case_id FK
    bool identity_match
    bool document_valid
    bool sanctions_hit
    bool pep_hit
    jsonb provider_raw
  }
  ELIGIBILITY_ASSESSMENTS {
    uuid id PK
    uuid application_id FK
    int risk_score "0-100"
    string decision "PASS/FAIL/REVIEW"
    string product_tier
  }
  RISK_FACTORS {
    uuid id PK
    uuid assessment_id FK
    string code
    int weight
    string description
  }
  ACCOUNTS {
    uuid id PK
    uuid user_id FK
    string account_number UK
    string ifsc
    string swift
    string currency
    string status "ACTIVE/FROZEN/CLOSED"
    numeric balance "cached; source of truth = ledger"
    timestamp opened_at
  }
```

### 1.2 UC2 — Money-movement domain

```mermaid
erDiagram
  FUNDING_REQUESTS ||--|| PAYMENTS : "creates"
  TRANSFERS ||--|| PAYMENTS : "creates"
  PAYMENTS ||--o{ LEDGER_ENTRIES : "posts (double-entry)"
  PAYMENTS ||--|| FRAUD_DECISIONS : "screened by"
  FRAUD_DECISIONS ||--o{ FRAUD_SIGNALS : "explained by"
  FRAUD_DECISIONS ||--o{ FRAUD_CASES : "may open"

  FUNDING_REQUESTS {
    uuid id PK
    uuid account_id
    numeric amount
    string currency
    string source "CARD/BANK/UPI"
    string status "PENDING/COMPLETED/REJECTED"
    string idempotency_key UK
    uuid trace_id
    timestamp created_at
  }
  TRANSFERS {
    uuid id PK
    uuid from_account_id
    string to_account_ref
    numeric amount
    string currency
    string rail "DOMESTIC/INTERNATIONAL"
    string status "INITIATED/APPROVED/REJECTED/SETTLED/HELD"
    string idempotency_key UK
    uuid trace_id
    timestamp created_at
  }
  PAYMENTS {
    uuid id PK
    string kind "FUNDING/TRANSFER"
    uuid source_ref "funding/transfer id"
    numeric amount
    string currency
    string status "PENDING/PROCESSED/FAILED"
    timestamp processed_at
  }
  LEDGER_ENTRIES {
    uuid id PK
    uuid payment_id FK
    uuid account_id
    string direction "DEBIT/CREDIT"
    numeric amount
    numeric balance_after
    timestamp created_at
  }
  FRAUD_DECISIONS {
    uuid id PK
    uuid payment_id
    int score "0-1000"
    string decision "APPROVE/REJECT/REVIEW"
    int latency_ms
    string model_version
    timestamp created_at
  }
  FRAUD_SIGNALS {
    uuid id PK
    uuid decision_id FK
    string code "VELOCITY/GEO/AMOUNT/DEVICE/BLACKLIST"
    numeric contribution
    string detail
  }
  FRAUD_CASES {
    uuid id PK
    uuid decision_id FK
    string status "OPEN/CONFIRMED/FALSE_POSITIVE/CLOSED"
    uuid assigned_to
    string resolution
    timestamp created_at
  }
```

### 1.3 Shared platform tables
```mermaid
erDiagram
  AUDIT_LOG {
    uuid id PK
    string event_type
    string producer
    uuid trace_id
    uuid actor_id
    jsonb payload "immutable"
    string prev_hash "hash-chain (tamper-evident)"
    string hash
    timestamp occurred_at
  }
  NOTIFICATIONS {
    uuid id PK
    uuid user_id
    string channel "EMAIL/SMS/PUSH"
    string template
    string status "QUEUED/SENT/FAILED"
    jsonb context
    timestamp created_at
  }
  OUTBOX {
    uuid id PK
    string topic
    jsonb envelope
    bool published
    timestamp created_at
  }
```
> **audit_log** is append-only and **hash-chained** (`hash = H(prev_hash + payload)`) → tamper-evident regulatory trail. Every service has an **outbox** table for the transactional-outbox pattern.

---

## 2. State machines

### 2.1 Onboarding application (onboarding-svc)
```mermaid
stateDiagram-v2
  [*] --> DRAFT
  DRAFT --> CAPTURED: submit customer info
  CAPTURED --> KYC_PENDING: kyc.requested
  KYC_PENDING --> KYC_PASSED: kyc.completed(PASS)
  KYC_PENDING --> KYC_FAILED: kyc.completed(FAIL)
  KYC_PENDING --> MANUAL_REVIEW: kyc.completed(REVIEW)
  KYC_PASSED --> ELIGIBLE: eligibility.evaluated(PASS)
  KYC_PASSED --> INELIGIBLE: eligibility.evaluated(FAIL)
  KYC_PASSED --> MANUAL_REVIEW: eligibility.evaluated(REVIEW)
  ELIGIBLE --> ACCOUNT_OPENED: account.opened
  ACCOUNT_OPENED --> [*]
  KYC_FAILED --> [*]
  INELIGIBLE --> [*]
  MANUAL_REVIEW --> ELIGIBLE: analyst approves
  MANUAL_REVIEW --> INELIGIBLE: analyst rejects
```

### 2.2 Transaction (funding/transfer via payment-svc)
```mermaid
stateDiagram-v2
  [*] --> PENDING
  PENDING --> SCREENING: sync fraud call
  SCREENING --> APPROVED: fraud APPROVE
  SCREENING --> REJECTED: fraud REJECT
  SCREENING --> HELD: fraud REVIEW
  SCREENING --> UNSCREENED: fraud timeout(>50ms) → async review
  APPROVED --> POSTED: ledger double-entry
  POSTED --> SETTLED: rail adapter ack
  HELD --> APPROVED: analyst clears
  HELD --> REJECTED: analyst confirms fraud
  UNSCREENED --> POSTED: provisional + async recheck
  REJECTED --> [*]
  SETTLED --> [*]
```

---

## 3. Key sequences

### 3.1 UC1 — end-to-end onboarding
```mermaid
sequenceDiagram
  autonumber
  participant U as React SPA
  participant GW as Gateway
  participant ON as onboarding-svc
  participant K as Kafka
  participant KY as kyc-svc
  participant EL as eligibility-svc
  participant AC as account-svc
  participant NO as notification-svc
  participant AU as audit-svc

  U->>GW: POST /onboarding (customer info) + JWT
  GW->>ON: forward (+X-User-Id, trace_id)
  ON->>ON: create application (CAPTURED)
  ON-->>K: customer.captured
  K-->>KY: consume
  KY->>KY: verify identity+docs+sanctions (Celery async)
  KY-->>K: kyc.completed(PASS, score)
  K-->>ON: update KYC_PASSED
  K-->>EL: consume kyc.completed
  EL->>EL: score risk + eligibility rules
  EL-->>K: eligibility.evaluated(PASS, tier)
  K-->>AC: consume
  AC->>AC: generate account number (instant)
  AC-->>K: account.opened(account_no, ifsc)
  K-->>ON: update ACCOUNT_OPENED
  K-->>NO: send welcome + account details
  K-->>AU: append audit (each step)
  U->>GW: GET /onboarding/{id} (poll/WebSocket)
  GW-->>U: status=ACCOUNT_OPENED + account details
```

### 3.2 UC2 — transfer with real-time fraud (the latency-critical path)
```mermaid
sequenceDiagram
  autonumber
  participant U as React SPA
  participant GW as Gateway
  participant TR as transfer-svc
  participant PAY as payment-svc
  participant FR as fraud-svc
  participant RED as Redis feature store
  participant K as Kafka
  participant NO as notification-svc
  participant OPS as ops-svc

  U->>GW: POST /transfers (Idempotency-Key) + JWT
  GW->>TR: forward
  TR->>TR: validate + persist (INITIATED)
  TR->>PAY: create payment (PENDING)
  PAY->>FR: POST /screen (sync, 50ms budget)
  FR->>RED: GET features (velocity/geo/device/amount)
  RED-->>FR: features (sub-ms)
  FR->>FR: rules + model score → decision
  FR-->>PAY: {decision, score, signals, latency_ms}
  alt APPROVE
    PAY->>PAY: post double-entry ledger (DEBIT/CREDIT)
    PAY-->>K: payment.processed
    K-->>TR: settle via rail adapter → SETTLED
    K-->>NO: notify "transfer sent"
  else REJECT
    PAY-->>K: payment.failed(reason=fraud)
    K-->>TR: mark REJECTED
    K-->>NO: notify "transfer blocked"
    K-->>OPS: open fraud case
  else timeout>50ms
    PAY->>PAY: mark UNSCREENED, provisional hold
    PAY-->>K: fraud.async_review
    K-->>OPS: queue for async screening
  end
  FR-->>K: fraud.completed (always, for audit+training)
```

### 3.3 Auth / token refresh
```mermaid
sequenceDiagram
  autonumber
  participant U as SPA
  participant GW as Gateway
  participant A as auth-svc
  U->>GW: POST /auth/login (email,pw)
  GW->>A: forward
  A-->>U: {access(15m), refresh(7d)} RS256
  U->>GW: API call + Bearer access
  GW->>GW: verify signature w/ public key (no auth-svc call)
  Note over GW: on 401 (expired)
  U->>GW: POST /auth/refresh (refresh)
  GW->>A: rotate → new access+refresh, blacklist old (Redis)
  A-->>U: new tokens
```

---

## 4. Fraud engine internals (fraud-svc)

```mermaid
flowchart TB
  IN[POST /screen<br/>payment ctx] --> FEAT[Feature assembler]
  FEAT --> RED[(Redis feature store<br/>velocity, last_geo, device_seen,<br/>amount_stats, blacklist)]
  FEAT --> RULES[Rule engine<br/>declarative YAML rules]
  FEAT --> MODEL[Score model<br/>pluggable · stub/LogReg/GBDT]
  RULES --> AGG[Aggregator<br/>weighted score + reason codes]
  MODEL --> AGG
  AGG --> DEC{Thresholds<br/>tunable}
  DEC -->|score &lt; T_low| APPROVE
  DEC -->|T_low..T_high| REVIEW
  DEC -->|score &ge; T_high| REJECT
  AGG --> OUT[Response + signals]
  AGG -.-> UPD[Update Redis velocity counters]
```

- **Feature store keys** (Redis, TTL-based): `vel:{account}:1m`, `vel:{account}:1h`, `geo:{account}:last`, `dev:{account}:{device_hash}`, `amt:{account}:stats`, `bl:{account_ref}`.
- **Rules** are declarative + versioned (e.g. `amount > 3× rolling_avg AND rail=INTERNATIONAL AND new_beneficiary → +400`). Each firing emits a **reason code** → explainability + false-positive tuning.
- **Reducing false positives:** ops feedback (`FALSE_POSITIVE`) decrements the offending rule's weight via ops-svc → fraud-svc rule-weight store; thresholds are per-segment.
- **Latency budget:** everything in-process + Redis; hard 50 ms timeout in payment-svc; on timeout → `UNSCREENED` fail-open with async re-screen (never blocks money indefinitely, never posts silently either).

---

## 5. Ledger correctness (payment-svc)

- **Double-entry:** every posting writes ≥2 `ledger_entries` that sum to zero (DEBIT source, CREDIT destination / system account). Balance = sum of entries; `accounts.balance` is a cache reconciled from the ledger.
- **Atomicity:** posting runs inside a single DB transaction with `SELECT … FOR UPDATE` on the account row → no double-spend under concurrency.
- **Idempotency:** `Idempotency-Key` (Redis + unique DB index) means a retried funding/transfer returns the original result, never a second debit.
- **Outbox:** the `payment.processed` event is written to the `outbox` table in the same transaction, then published by a relay → DB and Kafka never diverge.

---

## 6. Module/class layout (representative — transfer-svc)

```mermaid
classDiagram
  class TransferViewSet {
    +create(request)
    +retrieve(id)
    +list()
  }
  class TransferService {
    +initiate(dto, idem_key) Transfer
    +on_payment_processed(evt)
    +on_fraud_completed(evt)
  }
  class Transfer {
    <<Model>>
    uuid id
    uuid from_account_id
    string to_account_ref
    Money amount
    string rail
    string status
  }
  class PaymentClient {
    +create_payment(transfer) PaymentRef
  }
  class TransferEvents {
    +publish_initiated(t)
    +consume_payment_processed()
  }
  TransferViewSet --> TransferService
  TransferService --> Transfer
  TransferService --> PaymentClient
  TransferService --> TransferEvents
```
The same layering (`ViewSet → Service → Model/Client/Events`) is used by every service; see [`03-tech-stack.md`](03-tech-stack.md#3-repo-layout).

---

## 7. API surface (summary — full spec in `04-api-contracts.md`)

| Method & path | Service | Auth role |
|---------------|---------|-----------|
| `POST /auth/login`, `/auth/refresh`, `/auth/register` | auth | public |
| `POST /onboarding`, `GET /onboarding/{id}` | onboarding | customer |
| `POST /kyc/{app}/documents`, `GET /kyc/{app}` | kyc | customer |
| `GET /eligibility/{app}` | eligibility | customer/ops |
| `GET /accounts/{id}`, `GET /accounts/me` | account | customer |
| `POST /funding`, `GET /funding/{id}` | funding | customer |
| `POST /transfers`, `GET /transfers/{id}` | transfer | customer |
| `POST /fraud/screen` (internal), `GET /fraud/decisions/{id}` | fraud | internal/ops |
| `GET /ops/cases`, `PATCH /ops/cases/{id}` | ops | ops_analyst |
| `GET /audit?trace_id=` | audit | compliance |
| `GET /notifications/me` | notification | customer |
