# 03 · Tech Stack, Service Catalogue & Repo Layout

## 1. Stack decisions and *why*

| Concern | Choice | Why (vs. alternative) |
|---------|--------|-----------------------|
| Backend framework | **Django 5 + DRF** | Batteries-included (ORM, migrations, admin, auth), DRF gives serializers/viewsets/throttling for API-first. One project per service keeps blast radius small. |
| Language | **Python 3.12** | Team brief mandates Python-centric; async-capable, huge fraud/ML ecosystem. |
| Auth | **SimpleJWT, RS256** | Access + refresh out of the box, rotation + blacklist. RS256 (asymmetric) lets every service verify tokens with the **public key only** — no callback to auth-svc on the hot path. |
| API schema | **drf-spectacular** | Auto-generates OpenAPI 3 from serializers → generated TS clients for React → contract-first. |
| Event bus | **Apache Kafka** | Banking-grade durability, replay, partitioning by account for ordering, consumer groups. *Fallback:* Redis Streams (documented) if Kafka is too heavy for the hackathon box. |
| Background jobs | **Celery + Redis** | KYC doc processing, notification fan-out, ledger reconciliation, retries with backoff. |
| Primary DB | **PostgreSQL 16, DB-per-service** | ACID for money; row-level locking + `SELECT … FOR UPDATE` for ledger; JSONB for flexible KYC payloads. |
| Cache / fast state | **Redis** | Idempotency keys, rate limiting, JWT blacklist, **fraud feature store** (sub-ms lookups), Celery broker. |
| Object storage | **MinIO** (S3 API) | KYC document uploads; presigned URLs; swappable for AWS S3 in prod. |
| Realtime | **Django Channels + WebSocket** | Live ops dashboard (fraud alerts), live txn/notification status. |
| Edge / gateway | **Nginx** (+ DRF throttle per service) | TLS termination, routing, rate limiting, JWT pre-validation via `auth_request`. Kong/APISIX are drop-in upgrades. |
| Frontend | **React 18 + Vite + TS** | Fast dev server; TS for contract types; feature-folder ownership per member. |
| FE state/data | **React Query + Redux Toolkit** | React Query for server cache; RTK for auth/session UI state. |
| FE styling | **Tailwind CSS** (+ Headless UI) | Fast, consistent, no bikeshedding. |
| Containers | **Docker + docker-compose** (local), **Kubernetes + Helm** (prod) | One-command local stack; cloud-ready manifests. |
| IaC / CI-CD | **Terraform + GitHub Actions** | Reproducible infra; PR pipelines: lint → test → build → scan → deploy. |
| Observability | **OpenTelemetry → Prometheus + Grafana**, **Loki** logs, **Sentry** errors | Trace `trace_id` across services; latency SLOs (esp. fraud p99). |
| Secrets | **.env (local)** / **Vault or cloud secret manager (prod)** | RS256 private key, DB creds, Kafka SASL. |

### Added components beyond the brief (and why)
- **Redis feature store** — mandatory to hit *"fraud checks in milliseconds"*.
- **Double-entry ledger inside payment-svc** — correctness/audit for money movement.
- **Idempotency-Key middleware** — safe retries for funding/transfers (no double debit).
- **MinIO** — KYC needs somewhere to store ID documents.
- **Django Channels** — *"operational visibility"* + *"notify customers"* want realtime.
- **drf-spectacular + generated clients** — makes contract-first parallel work real.

---

## 2. Service catalogue (ports, DBs, topics)

| Service | Port | Owner | Reads | Publishes events | Consumes events |
|---------|------|-------|-------|------------------|-----------------|
| `api-gateway` (Nginx) | 8080 | M1 | — | — | — |
| `auth-svc` | 8001 | M1 | auth_db | `user.registered` | — |
| `onboarding-svc` | 8002 | M1 | onboarding_db | `customer.captured`, `onboarding.completed` | `kyc.completed`, `eligibility.evaluated`, `account.opened` |
| `kyc-svc` | 8003 | M2 | kyc_db, MinIO | `kyc.requested`, `kyc.completed` | `customer.captured` |
| `eligibility-svc` | 8004 | M2 | eligibility_db | `eligibility.evaluated` | `kyc.completed` |
| `account-svc` | 8005 | M2 | account_db | `account.opened` | `eligibility.evaluated` |
| `funding-svc` | 8006 | M3 | funding_db | `funding.requested`, `funding.completed` | `account.opened`, `payment.processed` |
| `transfer-svc` | 8007 | M3 | transfer_db | `transfer.initiated`, `transfer.settled` | `payment.processed`, `fraud.completed` |
| `payment-svc` (+ledger) | 8008 | M3 | payment_db | `payment.processed`, `payment.failed` | `funding.requested`, `transfer.initiated`, `fraud.completed` |
| `fraud-svc` | 8009 | M4 | fraud_db, Redis | `fraud.completed`, `fraud.flagged` | `funding.requested`, `transfer.initiated` (+ sync API) |
| `notification-svc` | 8010 | M2 | notify_db | `notification.sent` | `*.completed`, `account.opened`, `transaction.*` |
| `audit-svc` | 8011 | M4 | audit_db | — | **all** topics (`*`) |
| `ops-svc` | 8012 | M4 | reads fraud/audit | `case.updated` | `fraud.flagged`, `transaction.rejected` |

Event envelope + full payloads: [`04-api-contracts.md`](04-api-contracts.md).

---

## 3. Repo layout (monorepo)

```
banking-platform/
├── docker-compose.yml              # M4 owns; brings up ALL services + kafka+pg+redis+minio
├── .github/workflows/ci.yml        # M4 owns
├── infra/                          # M4: terraform, k8s/helm, prometheus, grafana dashboards
├── gateway/                        # M1: nginx.conf, routing, auth_request, rate limits
├── libs/                           # shared python packages (installed editable in each svc)
│   ├── common_auth/                # M1: JWT RS256 verify middleware, DRF permission classes
│   ├── common_events/              # M3: Kafka producer/consumer + event envelope + registry
│   ├── common_idempotency/         # M3: Idempotency-Key middleware backed by Redis
│   └── common_observability/       # M4: OTel setup, trace_id middleware, log config
├── services/
│   ├── auth_svc/                   # M1  (Django project)
│   ├── onboarding_svc/             # M1
│   ├── kyc_svc/                    # M2
│   ├── eligibility_svc/            # M2
│   ├── account_svc/                # M2
│   ├── notification_svc/           # M2
│   ├── funding_svc/                # M3
│   ├── transfer_svc/               # M3
│   ├── payment_svc/                # M3
│   ├── fraud_svc/                  # M4
│   ├── audit_svc/                  # M4
│   └── ops_svc/                    # M4
└── web/                            # React app (feature folders owned per member)
    └── src/features/
        ├── auth/                   # M1
        ├── onboarding/             # M1
        ├── kyc/  eligibility/  account/   # M2
        ├── funding/  transfers/  history/  # M3
        └── ops/  audit/  monitoring/       # M4
```

Every `services/<svc>/` is a standard Django project:

```
kyc_svc/
├── manage.py
├── Dockerfile
├── requirements.txt
├── config/            # settings, urls, celery, asgi/wsgi
└── <app>/             # models.py, serializers.py, views.py, services.py (business logic),
                       # events.py (produce/consume), tasks.py (celery), tests/
```

**Layering inside a service** (enforced in review): `views` (DRF, thin) → `services.py` (business logic) → `models` (ORM) / `events.py` (Kafka). Views never contain business rules; that lives in `services.py` so it's unit-testable without HTTP.

---

## 4. Local run (one command)

```bash
# from repo root
docker-compose up --build          # brings up gateway, all 12 services, kafka, pg, redis, minio, grafana
cd web && npm install && npm run dev   # React on :5173, proxied to gateway :8080
```

Health of the whole stack: `GET http://localhost:8080/healthz/aggregate` (gateway fans out to each `/readyz`).
