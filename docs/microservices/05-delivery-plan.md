# 05 · Delivery Plan

> How ten services get built by four people without deadlocking on each other,
> in what order, and how we prove each phase actually works.
>
> Phases are **dependency-ordered, not calendar-dated** — compress or stretch
> them to whatever timeline you're working to. What must not change is the
> *order*, because each phase unblocks the next.

---

## 1. Build order — the dependency spine

```mermaid
flowchart LR
  P0["P0 · Foundation<br/>platform_common<br/>compose · DBs · CI"]:::p0
  P1["P1 · Spine<br/>identity + account<br/>+ ledger"]:::p1
  P2["P2 · Money path<br/>payments + fraud<br/>(the demo)"]:::p2
  P3["P3 · Backbone<br/>notification + audit<br/>+ ops"]:::p3
  P4["P4 · UC1<br/>onboarding + kyc"]:::p4
  P5["P5 · Hardening<br/>chaos · load · security<br/>· admin tooling"]:::p5
  P0 --> P1 --> P2 --> P3 --> P5
  P1 --> P4 --> P5
  classDef p0 fill:#eceff1,stroke:#607d8b;
  classDef p1 fill:#e3f2fd,stroke:#1976d2;
  classDef p2 fill:#e8f5e9,stroke:#43a047;
  classDef p3 fill:#fce4ec,stroke:#d81b60;
  classDef p4 fill:#fff8e1,stroke:#f9a825;
  classDef p5 fill:#f3e5f5,stroke:#8e24aa;
```

**P2 is the demo.** If everything after it slipped, you could still show the
headline capability: a real transfer, screened in milliseconds, approved or
blocked, with money moving through a double-entry ledger. Build in this order so
that the thing worth showing exists early.

### Phase gates — a phase is done when this is true, not when the code is written

| Phase | Gate |
|---|---|
| **P0** | `docker compose up` starts Postgres + a hello-world service; `platform_common` publishes an event to itself through the full outbox→relay→inbox→handler path; CI runs tests on every service. |
| **P1** | A user registers, logs in, gets an account, and `GET /api/accounts/{id}` returns a real ledger balance. Two concurrent debits on one account: exactly one succeeds. |
| **P2** | A transfer runs the full saga. A rule-tripping transfer is `BLOCKED` and the hold is released. A borderline transfer is `UNDER_REVIEW`, an analyst approves it, and the saga resumes. `FraudDecision.latency_ms` p99 < 50 ms. |
| **P3** | Every state change appears in the audit chain; `verify_chain` passes. The customer sees notifications. The ops dashboard shows queue depth per service — including for a service you have deliberately killed. |
| **P4** | A new customer onboards from scratch, KYC runs async, an account opens, and that account funds and transfers. UC1 and UC2 connect. |
| **P5** | Chaos tests pass (§6). Load test holds 250 TPS. Authz matrix test passes for every role × endpoint. A fraud rule is added in shadow mode, measured, and promoted — live. |

---

## 2. Team split — four people, ten services

Ownership is by **vertical slice** (service + its React area + its tests), so
nobody waits on anyone for a full feature.

| | **M1 · Platform & Identity** | **M2 · Accounts & Onboarding** | **M3 · Money** | **M4 · Fraud & Operations** |
|---|---|---|---|---|
| **Services** | `platform_common`, identity-svc, gateway | account-svc, onboarding-svc, kyc-svc | payments-svc, ledger-svc | fraud-svc, notification-svc, audit-svc, ops-svc |
| **React** | app shell, auth, routing, API client | onboarding wizard, dashboard, beneficiaries | funding, transfers, schedules, history | analyst console, ops dashboard, admin (rules + limits + reports) |
| **Owns for the team** | The event backbone, `ServiceClient`, auth, compose, CI | The UC1 decision logic | **Money correctness** — ledger invariants, idempotency, saga | The rule engine + the audit chain |
| **Hardest thing** | Getting outbox→inbox right once, for everyone | Limit reservation under concurrency | Saga compensation on every failure path | 50 ms budget without Redis |

**Load is deliberately uneven in service count and even in difficulty.** M4 owns
four services, but notification/audit/ops are each one model plus a handler; the
fraud engine is the real work. M3 owns two services that carry the most
correctness risk in the system.

### 2.1 Coordination rules

1. **M1 ships `platform_common` first, and it is frozen at the P1 gate.** Everyone builds against a stable library. Changes after that go through M1 with a version bump.
2. **Contracts before code.** Before implementing a cross-service call, add it to [`04-api-contracts.md`](04-api-contracts.md) and to the provider's OpenAPI. The consumer then codes against a stub.
3. **Stub your dependencies.** `platform_common.testing` ships a `FakeServiceClient` with canned responses per endpoint. M3 builds the saga against a fake fraud-svc on day one; wiring in the real one is a config change.
4. **Never import another service's models.** There is no import path — separate databases and separate Django projects make the mistake impossible. That's the point.
5. **One person owns each migration.** Migrations live inside a service; conflicts are impossible across services by construction.

---

## 3. Repository & local environment

```
deploy/
├── docker-compose.yml
├── docker-compose.core.yml       # --profile core: the 6 services needed for the UC2 demo
├── nginx/gateway.conf
├── postgres/init-databases.sql   # 10 databases, 10 roles, REVOKE CONNECT across them
└── seed/                         # deterministic demo data: customers, accounts, rules, cases
```

```yaml
# deploy/docker-compose.yml  (shape — one pair per service)
x-django: &django
  build: { context: .., dockerfile: services/${SVC}/Dockerfile }
  env_file: [.env]
  depends_on: { postgres: { condition: service_healthy } }

services:
  postgres:
    image: postgres:16
    volumes: ["./postgres/init-databases.sql:/docker-entrypoint-initdb.d/init.sql", "pgdata:/var/lib/postgresql/data"]
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U postgres"], interval: 5s, retries: 10 }

  payments-web:
    <<: *django
    command: gunicorn config.wsgi --bind 0.0.0.0:8000 --workers 3
    environment: { DATABASE_URL: postgres://payments_role:...@postgres:5432/payments_db }
    profiles: ["core", "all"]
  payments-worker:                      # separate container: scales on a different signal
    <<: *django
    command: python manage.py qcluster
    environment: { DATABASE_URL: postgres://payments_role:...@postgres:5432/payments_db, Q_WORKERS: 6 }
    profiles: ["core", "all"]
  # … ×10 services

  gateway:
    image: nginx:alpine
    volumes: ["./nginx/gateway.conf:/etc/nginx/conf.d/default.conf:ro", "../frontend/dist:/usr/share/nginx/html:ro"]
    ports: ["8080:80"]
    profiles: ["core", "all"]
```

```sql
-- deploy/postgres/init-databases.sql
CREATE ROLE payments_role LOGIN PASSWORD :'payments_pw';
CREATE DATABASE payments_db OWNER payments_role;
REVOKE CONNECT ON DATABASE payments_db FROM PUBLIC;   -- ← isolation is enforced, not agreed
-- … ×10

-- ledger: postings are immutable
REVOKE UPDATE, DELETE ON ledger_posting, ledger_journalentry FROM ledger_role;
-- audit: the log is append-only, enforced by the database
REVOKE UPDATE, DELETE ON audit_auditlog FROM audit_role;
```

> **`REVOKE UPDATE, DELETE` on the audit log and ledger postings is the single
> highest-value line in the whole deployment.** It converts "we promise the
> audit trail is append-only" into something a regulator can verify in one
> `\dp` — and it costs nothing.

### 3.1 Resource reality

21 containers is ~3–4 GB of RAM. Two escape hatches, both provided:

| Mode | Command | What runs |
|---|---|---|
| Core | `docker compose --profile core up` | identity, account, payments, ledger, fraud, notification + Postgres + gateway — the whole UC2 demo |
| Full | `docker compose --profile all up` | All ten services |
| Lite | `RUN_WORKER_INLINE=1 docker compose --profile core up` | `honcho` runs `gunicorn` + `qcluster` in one container per service (~halves the container count) |
| Test | `pytest` with `Q_SYNC=1` | No workers at all — tasks run inline |

---

## 4. Seed data — designed for demonstrability

Deterministic, so demos are repeatable and every path is reachable on command.

| Persona | Setup | Demonstrates |
|---|---|---|
| **Asha** (clean customer) | 412-day-old account, ₹498,200, 3 known beneficiaries, healthy profile | The happy path: instant internal transfer, `ALLOW`, score ~20 |
| **Rohit** (borderline) | New beneficiary added 3 h ago, in cooling-off, international | `REVIEW` → analyst console → approve → saga resumes |
| **Vikram** (blocked) | Beneficiary on the internal blacklist | Hard `BLOCK` (R006), hold released, balance unchanged |
| **Meera** (velocity) | Script fires 6 transfers in 4 minutes | R002 + R004 fire; the last transfers trip velocity and fan-out |
| **Sana** (new customer) | No account | Full UC1: onboard → KYC → eligibility → account → fund → transfer |
| **Deepak** (returns) | Transfer to a closed foreign account | Rail return → compensating reversal → funds back → ops failure case |
| **analyst@ing.demo** | `FRAUD_ANALYST` | Case queue, SLA ageing, false-positive marking |
| **ops@ing.demo** | `OPS` | Queue monitor, failure investigation, DLQ replay |
| **admin@ing.demo** | `ADMIN` | Rule editor with shadow mode, limit policies, reports |

The fraud simulators are **deterministic by input hash** — the same customer,
the same amount, always the same decision. Nothing in the demo depends on luck.

---

## 5. Demo script (12 minutes)

| # | Time | Show | Point being made |
|---|---|---|---|
| 1 | 0:00 | Sana onboards: form → KYC pending → account number appears | UC1 end to end, async KYC, instant account |
| 2 | 2:00 | Sana funds ₹50,000 from a debit card | Funding is screened too — not just outbound |
| 3 | 3:00 | Asha transfers ₹5,000 internally · settles instantly · `latency_ms: 9` | Real-time; internal transfers genuinely are instant |
| 4 | 4:00 | Vikram transfers to a blacklisted payee → blocked, **balance unchanged** | Hard block + hold released; money never moved |
| 5 | 5:00 | Rohit transfers international to a 3-hour-old payee → `UNDER_REVIEW` | The interesting case — funds held, not lost |
| 6 | 6:00 | Analyst console: reason codes, feature vector, **per-rule precision** | Explainability, and why analysts trust it |
| 7 | 7:30 | Analyst approves → the transfer completes without re-entry | Async saga resumption across services |
| 8 | 8:30 | Admin: add a rule in **shadow mode**, `POST /rules/{id}/test` shows it would have fired 312 times at 0.77 precision | Tune a live fraud engine with zero customer risk |
| 9 | 9:30 | Admin lowers R005's weight (precision 0.34) → next borderline transfer allows | The false-positive loop, closed |
| 10 | 10:30 | **Kill fraud-svc**, submit a transfer → `UNDER_REVIEW`, ops shows UNREACHABLE | Fails to review, never to allow (ADR-005) |
| 11 | 11:15 | Ops: queue depths, DLQ, one correlation id reconstructing the whole transfer across 10 services | Operational visibility + the audit trail |
| 12 | 11:45 | `verify_chain` passes; show `REVOKE UPDATE, DELETE` | Tamper-evidence you can prove, not claim |

Steps 10 and 12 are what separate this from a CRUD app with an `if amount >
10000` check.

---

## 6. Verification plan

### 6.1 The tests that matter most

```python
def test_concurrent_debits_cannot_overdraw():
    """The invariant that a demo can't show but a regulator will ask about."""
    account = seed_account(balance="1000.00")
    results = run_concurrently(lambda: transfer(account, "800.00"), times=2)
    assert sorted(r.status for r in results) == ["BLOCKED_OR_FAILED", "SETTLED"]
    assert balance(account) == Decimal("200.00")

def test_idempotency_key_survives_ten_concurrent_submits():
    key = uuid4()
    results = run_concurrently(lambda: post_transfer(key=key, amount="500.00"), times=10)
    assert len({r.json()["id"] for r in results}) == 1
    assert Transaction.objects.filter(idempotency_key=key).count() == 1
    assert postings_for(key).count() == 2            # exactly one DR and one CR

@hypothesis.given(entries=random_journal_entries())
def test_ledger_always_balances(entries):
    for e in entries:
        post_entry(**e)
    for ccy in currencies():
        assert sum_debits(ccy) == sum_credits(ccy)
    assert not Balance.objects.filter(ledger_account__kind="CUSTOMER", ledger_balance__lt=0).exists()

def test_fraud_p99_under_budget(benchmark):
    latencies = [screen(random_request()).latency_ms for _ in range(1000)]
    assert percentile(latencies, 99) < 50

def test_rule_dsl_cannot_execute_code():
    for payload in ["__import__('os').system('id')", {"fact": "__class__"}, {"op": "eval"}]:
        with pytest.raises(ValidationError):
            RuleSerializer(data={"condition": payload, ...}).is_valid(raise_exception=True)
```

### 6.2 Chaos scenarios — run before every demo

| Kill | Expected | Proves |
|---|---|---|
| `fraud-svc` mid-transfer | All new transfers → `UNDER_REVIEW`, holds intact, no money moves | ADR-005, fail-to-review |
| `ledger-svc` after a hold, before capture | Saga → `COMPENSATION_PENDING`; retry task releases the hold; hold TTL as backstop | Compensation + defence in depth |
| `notification-svc` for 10 min | Payments unaffected; outbox accumulates; all messages arrive on recovery | I6, at-least-once |
| A publisher between COMMIT and relay (`kill -9`) | The 1-minute sweeper delivers the event | Outbox durability |
| Deliver the same event 50× | One notification, one audit row, one projection update | Inbox effect-once |
| `audit-svc` for 30 min | Zero audit rows lost; all replay on recovery | Backbone durability |
| Corrupt one audit row by hand | `verify_chain` fails and names the row | Tamper-evidence works |

### 6.3 Load test

```
locust: 250 TPS mixed (70% internal, 20% domestic, 10% international)
        over 500 accounts, 15 minutes
assert: transfer p99 < 400 ms · fraud p99 < 50 ms
        outbox_pending never > 500 · event lag p95 < 2 s
        zero ledger invariant breaches · zero DEAD outbox rows
```

---

## 7. Requirements traceability

Every requirement from the brief, and where it is satisfied. This is the table to
read if you want to check nothing was dropped.

### 7.1 Customer

| Requirement | Where | Evidence |
|---|---|---|
| Add money to account | payments-svc funding saga | [`02-lld.md` §7.8](02-lld.md) |
| Select funding source (bank / debit card / wallet) | `FundingSource` + per-source adapters | [`02-lld.md` §7.8](02-lld.md) |
| Verify transaction & get confirmation | Source verification + `funding.settled` → notification | [`03-events.md` §4](03-events.md) |
| Internal transfer (ING → ING) | `rail=INTERNAL`, settles at capture | [`02-lld.md` §8.4](02-lld.md) |
| Domestic transfer | `rail=DOMESTIC` + clearing accounts | [`02-lld.md` §8.4](02-lld.md) |
| International transfer | `rail=INTERNATIONAL` + FX position legs | [`02-lld.md` §8.4](02-lld.md) |
| Scheduled transfer (future-dated) | `TransferSchedule` + Q2 sweeper | [`02-lld.md` §7.6](02-lld.md) |
| Recurring transfer (automatic) | `TransferSchedule.frequency` | [`02-lld.md` §7.6](02-lld.md) |
| Secure login | identity-svc RS256 + rotation + reuse detection + TOTP | [`02-lld.md` §3](02-lld.md) |
| Dashboard | account-svc + React customer shell | [`02-lld.md` §13](02-lld.md) |
| Check balance | `GET /accounts/{id}/balance` (ledger-backed, staleness-aware) | [`04-api-contracts.md` §4](04-api-contracts.md) |
| Transaction history | Cursor-paginated `GET /api/transfers` | [`04-api-contracts.md` §5](04-api-contracts.md) |
| Track transfer status | Status timeline + polling | [`04-api-contracts.md` §5](04-api-contracts.md) |
| Manage beneficiaries | account-svc + cooling-off control | [`02-lld.md` §6](02-lld.md) |
| Cancel transfers | State-guarded cancel + compensation | [`02-lld.md` §7.2](02-lld.md) |

### 7.2 Fraud analyst · operations · administrator

| Requirement | Where |
|---|---|
| View fraud alerts | `GET /api/fraud/cases` + analyst console |
| Review suspicious transactions | Case detail: feature vector, reason codes, per-rule precision, audit trail |
| Manually approve / reject | `POST /cases/{id}/approve\|reject` → saga resumes or compensates |
| Monitor transfer queues | ops-svc polls `/internal/metrics`; snapshots survive outages |
| Monitor & investigate failed transfers | `FailureCase` + correlation-id trail + retry/DLQ replay |
| Configure fraud engine rules | Versioned JSON DSL, shadow mode, dry-run against history |
| Manage transaction & account limits | `LimitPolicy` (tier or account scope, per rail) |
| Generate system reports | Async Q2 report jobs → downloadable artefacts |

### 7.3 System-level

| Requirement | Where |
|---|---|
| Create funding requests, verify source, process, generate reference | payments-svc funding saga; `TXN-…` / `FND-…` references |
| Real-time rule-based fraud screening | fraud-svc, synchronous, p99 < 50 ms, measured per decision |
| Vectors: amount, velocity, location, device, blacklist, high-risk country, frequency | R001–R015 starter ruleset ([`02-lld.md` §9.5](02-lld.md)) |
| Rules: threshold, 5-minute window, fan-out to many beneficiaries, new payee + large amount | R001, R002, R004, R005 — exactly the brief's examples, as configuration |
| ALLOW / REVIEW / BLOCK for every transaction | `FraudDecision.decision`, one row per screening, never optional |
| Reduce false positives | Per-rule precision + analyst feedback + shadow mode + weight tuning |
| Instant customer notification | `payment.*` → notification-svc, ~1 s |
| Complete audit trail | Hash-chained append-only log, DB-enforced |
| Operational visibility & compliance | ops dashboards, correlation-id reconstruction, audit export |

### 7.4 Solution requirements

| Requirement | Met | How |
|---|---|---|
| Python-centric, backend-heavy | ✅ | 10 Django services; React is a thin client |
| Vite React + Django | ✅ | As specified |
| **Django Q2 as the message queue** | ✅ | ORM broker per service + outbox/relay/inbox — [`03-events.md`](03-events.md) |
| **No Redis / Python-only stack** | ✅ | Postgres + Django only; Nginx is config, with a Django fallback (ADR-008) |
| Microservices | ✅ | 10 services, DB-per-service, no shared models, independently deployable |
| API-first | ✅ | OpenAPI 3 per service, contract-tested in CI, generates the SPA's types |
| Event-driven | ✅ | 38 event types, transactional outbox, at-least-once + effect-once |
| Scalable | ✅ | Stateless tiers, independent scaling, 250 TPS budget with headroom |
| Secure | ✅ | RS256 + JWKS, service tokens, RBAC + ownership, field-level PII encryption, no-`eval` rule DSL, DB-enforced immutability |
| Banking-grade | ✅ | Double-entry, hold/capture, idempotency everywhere, saga compensation, hash-chained audit, invariant checks |
| Cloud-ready | ✅ | 12-factor, containerised, stateless, env-configured, K8s path documented |

---

## 8. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Django Q2 ORM-broker semantics differ from assumptions (`max_attempts`, `catch_up`, lease behaviour) | Medium | High — the backbone depends on them | **Spike this first, in P0.** Pin the version. Our own backoff lives in the outbox column, so we depend on Q2 for as little as possible |
| Saga compensation has an untested path | Medium | High | Every step's `undo` has a chaos test; `COMPENSATION_PENDING` is a first-class state with a sweeper |
| 21 containers overwhelm dev laptops | High | Medium | `core` profile + inline-worker mode, both shipped in P0 |
| Fraud latency budget missed under load | Low | Medium | Three queries + memory lookups measured in P2; `FraudDecision.latency_ms` makes regressions visible immediately |
| Cross-service debugging is painful | High | Medium | Correlation id in every log, event and audit row from P0 — retrofitting this later is miserable |
| Ten services is too much for four people | Medium | High | Build order puts the demo at P2; notification/audit/ops are deliberately tiny; ADR-002 documents folding ledger into payments if time runs out |
| Audit hash chain serialises writes | Low | Medium | Measured at 3× peak in P5; per-aggregate chains are the fallback |

---

## 9. Definition of done — per service

- [ ] Models + migrations; DB role has only the grants it needs
- [ ] DRF endpoints match [`04-api-contracts.md`](04-api-contracts.md); OpenAPI committed
- [ ] Publishes its events through `platform_common.publish()` **inside** the business transaction
- [ ] Consumes its events idempotently, with the sequence guard
- [ ] `Q_CLUSTER` configured; schedules registered via `register_schedules`
- [ ] `/healthz`, `/readyz`, `/internal/metrics` respond
- [ ] Correlation id propagated in; JSON logs out; **no PII in logs**
- [ ] Unit + integration tests; contract test green
- [ ] Its React area renders real data, including error and loading states
- [ ] Dockerfile builds; runs in compose; seeded data exercises every path
