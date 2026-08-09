# 00 · First Principles — how this architecture was derived

> This document exists so that nobody has to take the architecture on faith.
> Every service boundary, every synchronous call, and every queue in
> [`01-hld.md`](01-hld.md) is derived here from a requirement or a physical
> constraint. If you disagree with a boundary, disagree with the reasoning in
> §3 — don't argue about the box on the diagram.

**Constraints handed to us (non-negotiable):**

| Constraint | Source |
|---|---|
| Python-centric, backend-heavy | Requirement |
| Vite + React frontend, Django + DRF backend | Requirement |
| **Django Q2** is the message queue | Requirement |
| **No Redis, no Kafka** — company environment, Python-only dependencies | Environment |
| Microservices (not a modular monolith) | Requirement |
| API-first, event-driven, scalable, secure, banking-grade | Requirement |

---

## 1. Start with the numbers, not the diagram

The brief says *"millions of daily transactions."* Before drawing anything, work
out what that actually means, because it decides whether we need exotic
machinery or not.

```
1,000,000 txn/day ÷ 86,400 s  =  11.6 TPS average
Retail banking peak factor    ≈  8–10× average (payday, lunchtime, month-end)
                              →  ~100–120 TPS peak
Design headroom 2×            →  target 250 TPS sustained
```

**250 TPS is not a hard number for PostgreSQL.** A single well-indexed Postgres
instance does tens of thousands of simple writes per second. The binding
constraint is *not* throughput — it is:

1. **Row contention** on hot accounts (two transfers from the same account must
   serialise). Different accounts don't contend at all, so this scales with
   customer count, not TPS.
2. **Fraud decision latency** — a synchronous gate on every payment.
3. **Correctness under partial failure** — the thing that actually kills banking
   systems.

> **Consequence:** we do *not* need Kafka, a streaming feature store, sharding,
> or CQRS-everywhere. Those exist to solve throughput problems we do not have.
> Removing Redis costs us nothing at this scale. We spend our complexity budget
> on **correctness and isolation** instead. This is the single most important
> sentence in the design.

---

## 2. Invariants — the things that must never be false

Architecture is the set of structures that make invariants cheap to hold. Here
are ours, and what each one forces.

| # | Invariant | What it forces |
|---|---|---|
| **I1** | Money is never created or destroyed. Every movement has equal debits and credits. | A **double-entry ledger** with a **single writer**. → `ledger-svc` exists and no other service writes balances. |
| **I2** | No money moves before a fraud decision exists for it. | Fraud is a **synchronous gate** on the money path, not a listener. → `fraud-svc` is called request/response, not via a queue. |
| **I3** | A retried request must not move money twice. | **Idempotency keys** on every money-mutating POST, stored with the response. |
| **I4** | A customer's available balance can never go negative, even under concurrent transfers. | **Reserve-then-capture (hold)** semantics + row locks in a deterministic order. |
| **I5** | Every state change is reconstructible and provably untampered. | **Append-only, hash-chained audit log** in a service whose DB role has no `UPDATE`/`DELETE` grant. |
| **I6** | A side effect that fails (email down, ops dashboard down) must never fail or slow a payment. | Side effects are **asynchronous and at-least-once** → the outbox/queue backbone. |
| **I7** | If the fraud engine is unavailable, we must not silently approve. | **Fail to REVIEW**, never fail-open. Holds stay in place; ops is alerted. |
| **I8** | A transaction that is blocked, reviewed, or reversed must leave the customer's balance exactly as it was. | Every saga step has a **named compensating action**. |

Everything in the HLD traces to one of I1–I8.

---

## 3. Deriving the service boundaries

### 3.1 The wrong way

The tempting decomposition is "one service per noun in the requirements":
funding-svc, transfer-svc, beneficiary-svc, limit-svc, report-svc… That's how
you get twelve services, three of which sit on a single money path adding two
network hops and two failure modes per payment, for zero benefit.

### 3.2 The test we actually used

A boundary is justified only if it passes **at least two** of these four:

- **(a) Different reason to change.** Would a product change hit both sides at once? If yes, it's one service.
- **(b) Different data ownership.** Does one side need to read the other's tables to do its job? If yes, it's one service.
- **(c) Different scaling or latency profile.** Do they need to scale independently, or does one have a latency budget the other doesn't?
- **(d) Different blast radius / trust level.** Should a bug or compromise on one side be unable to touch the other's data?

### 3.3 Applying the test

| Candidate | (a) Reason to change | (b) Data | (c) Scaling/latency | (d) Blast radius | Verdict |
|---|---|---|---|---|---|
| **identity** | Auth policy, MFA, token lifetimes | users, credentials, keys | Read-heavy, low volume | Credential compromise must not reach money | ✅ **Service** |
| **onboarding** | Product/eligibility rules | applications | Low volume, long-lived workflow | — | ✅ **Service** |
| **kyc** | Compliance regime, vendor swaps | PII, ID documents | Slow (async, seconds-to-minutes) | Highest-sensitivity PII, must be isolated | ✅ **Service** |
| **eligibility** | Same as onboarding — product rules | Reads only the application + KYC result; owns nothing durable | Same request cycle as onboarding | Same | ❌ **Module inside onboarding-svc** (see ADR-004) |
| **account** | Product structure, limits policy | accounts, beneficiaries, limits | Read-heavy | — | ✅ **Service** |
| **funding** vs **transfer** | Both change when a rail or a payment product changes | Both are "payment orders" over the same lifecycle | Identical | Identical | ❌ **One `payments-svc`** (ADR-003) |
| **ledger** | Almost never — this is the most stable code in the system | Owns balances exclusively (I1) | Write-hot, lock-bound | A bug here is an existential event; nothing else may write it | ✅ **Service** |
| **fraud** | Constantly — rules are tuned weekly | rules, decisions, cases, its own feature read model | Hard 50 ms budget; scales with TPS | Analyst-facing, admin-writable rules | ✅ **Service** |
| **notification** | Channels, templates, providers | messages, preferences | Bursty, retry-heavy, slow I/O | Third-party creds | ✅ **Service** |
| **audit** | Compliance retention | append-only log | Write-heavy, never on hot path | Must be write-only to everyone | ✅ **Service** |
| **ops** | Ops tooling needs | read models + reports | Low volume | Privileged read across the estate | ✅ **Service** |

**Result: 10 services.** The two most interesting outcomes are the two *merges* —
funding+transfer into one, eligibility into onboarding — because resisting
unnecessary splits is what keeps a microservice system operable.

### 3.4 The one boundary worth arguing about

**Why is `ledger-svc` separate from `payments-svc`?** It costs a synchronous hop
on the money path (~5 ms), which is a real price.

We pay it because of (b) and (d): the ledger must be the **only** writer of
balances (I1), and the cheapest way to *guarantee* that — rather than hope for it
in code review — is to put balances in a database that `payments-svc` has no
credentials for. `payments-svc` changes every time we add a payment product;
`ledger-svc` should go months untouched. Different change cadence + a
correctness invariant that benefits from enforced isolation = split.

The alternative (ledger as a module inside payments) is *faster* and perfectly
defensible for a smaller system. It is documented as the fallback in ADR-002.

---

## 4. Deriving the communication style

The rule, applied mechanically to every interaction:

> **Synchronous** if and only if the caller cannot correctly proceed without the
> answer, *and* the answer protects an invariant.
> **Asynchronous** otherwise — always.

| Interaction | Sync? | Why |
|---|---|---|
| payments → account (validate account, beneficiary, limits) | **Sync** | Can't create a valid order without it |
| payments → ledger (place hold) | **Sync** | I4 — must know funds are reserved before screening |
| payments → fraud (screen) | **Sync** | I2 — money must not move unscreened |
| payments → ledger (capture) | **Sync** | Customer is waiting for a definitive status |
| payments → rail adapter (dispatch) | **Async** | SWIFT/ACH settle in hours or days. Physics, not preference. |
| onboarding → kyc (verify) | **Async** | Document processing is seconds-to-minutes |
| anything → notification | **Async** | I6 |
| anything → audit | **Async** | I6 |
| anything → ops | **Async** | I6 |
| fraud case resolved → payments (resume saga) | **Async** | Analyst review takes minutes-to-hours |

Only **four** synchronous hops sit on the money path. That's the latency budget
in §5, and it's deliberately small.

---

## 5. Deriving the latency budget

The customer-facing promise is "instant approve/reject with notification." Work
backwards from a p99 of 400 ms for `POST /transfers`:

```
  gateway + TLS + routing                  ~10 ms
  payments-svc: parse, idempotency, persist ~25 ms
  → account-svc: validate + limit check     ~35 ms   (sync)
  → ledger-svc: place hold (row lock)       ~45 ms   (sync)
  → fraud-svc: screen                       ~50 ms   (sync)  ← hard budget
  → ledger-svc: capture hold                ~60 ms   (sync)
  payments-svc: persist, emit outbox        ~25 ms
  serialisation + network jitter            ~90 ms
  ────────────────────────────────────────────────
  p99 total                                 ~340 ms  ✅ under 400 ms
```

**The 50 ms fraud budget is the one that constrains a design decision.** Without
Redis we cannot use an in-memory feature store, so the budget must be met with
Postgres and process memory:

```
  HTTP in + JSON parse (keep-alive, pooled)   ~4 ms
  AccountProfile read (single-row PK, cached) ~1 ms
  Velocity query (index range scan, 5 min)    ~2 ms
  Beneficiary novelty (unique index probe)    ~1 ms
  Rule evaluation (rules held in memory)      <1 ms
  Persist FraudDecision                       ~3 ms
  Response                                    ~2 ms
  ────────────────────────────────────────────────
  p99                                        ~14 ms  ✅ well under 50 ms
```

This is why fraud-svc maintains **its own denormalised read model** rather than
querying payments-svc or ledger-svc — a network hop inside a 50 ms budget is
unaffordable, and it would also violate (b) data ownership. Redis would have
saved ~3 ms. We didn't need it.

---

## 6. Deriving the event backbone from Django Q2

**The problem:** Django Q2 is a *task queue*, not a publish/subscribe broker. It
has no topics, no consumer groups, no fan-out, and no cross-service delivery. It
executes a named Python callable on a worker that shares the producer's codebase
and database.

**The constraint:** every service owns its own database (§3), and there is no
Redis to act as shared transport. So `Q_CLUSTER = {"orm": "default"}` — each
service's queue is a table inside its *own* Postgres. Service A therefore
*cannot* enqueue into service B's queue: it has no credentials for B's database,
by design.

**Therefore the transport between services must be HTTP**, and Django Q2's job is
to make each side of that hop durable and non-blocking. Which gives exactly one
sensible shape:

```
┌─ publisher (payments-svc) ──────────┐        ┌─ subscriber (notification-svc) ─┐
│                                     │        │                                 │
│  business txn ─┬─ state change      │        │  POST /internal/events          │
│   (atomic)     └─ OutboxEvent row   │        │        │                        │
│                     │               │        │        ├─ InboxEvent row        │
│        transaction.on_commit        │        │        │   (unique event_id)    │
│                     ↓               │        │        └─ 202 Accepted ─────────┼──▶
│              async_task(relay)      │        │             │                   │
│                     ↓               │        │   transaction.on_commit         │
│         Q2 worker: HTTP POST ───────┼───────▶│             ↓                   │
│                     ↑               │        │       async_task(dispatch)      │
│         Schedule(1 min) sweeper     │        │             ↓                   │
│         (safety net, backoff)       │        │       Q2 worker: handler        │
└─────────────────────────────────────┘        └─────────────────────────────────┘
```

Each half is derived, not chosen:

| Element | Derived from |
|---|---|
| **Outbox table written in the same DB transaction as the state change** | You cannot atomically write to Postgres *and* send an HTTP request. Dual-write = lost or phantom events. The outbox is the only correct answer. |
| **`on_commit` → `async_task` for the immediate send** | Q2's own `Schedule` granularity is minutes; a 60 s notification delay is unacceptable. The immediate path is the fast path. |
| **A 1-minute `Schedule` sweeper as well** | The in-memory `on_commit` hook is lost if the process dies between commit and enqueue. The sweeper makes delivery *eventually certain*; the fast path makes it *usually instant*. Both are needed. |
| **Inbox table with `unique(event_id)`** | HTTP retries mean at-least-once delivery. The unique index is what converts that into effect-once processing (I3 again, at the event layer). |
| **Subscriber returns 202 immediately, processes on its own queue** | A slow handler must not hold the publisher's relay worker or trigger a spurious retry (I6). |
| **Backoff state (`attempts`, `next_attempt_at`) on the outbox row, not Q2's `retry`** | Q2's `retry` is a *lease timeout* for re-delivery, not exponential backoff. Backoff is our concern, so we own it in a column. |

**What we give up versus Kafka, stated honestly:** no replay from an infinite
log, no strict per-partition ordering, and fan-out cost is O(subscribers) HTTP
calls per event rather than one append. At ~12 events/sec average, O(subscribers)
is irrelevant. Ordering is handled by making handlers order-tolerant (§7).
Replay is handled by the outbox and audit log both being durable and queryable.

---

## 7. Deriving the consistency model

Parallel Q2 workers mean **events can arrive out of order**. Two options:

1. Enforce ordering (single worker per aggregate, sequence gates, parking). Complex, throughput-limiting.
2. Make handlers order-tolerant. Every event carries `aggregate_id` + monotonically increasing `sequence`; handlers apply a state-machine guard and **ignore any event that would move the aggregate backwards**.

We take **(2)**, because at our volume ordering conflicts are rare and the
guard is three lines of code. Combined with the inbox unique index, every
handler is both idempotent and commutative-enough.

For the money path itself, ordering is *not* eventual — it is enforced
synchronously inside the saga (§4), so the ledger never sees a reordered
instruction. Eventual consistency applies only to read models, notifications,
audit, and ops dashboards, where a sub-second lag is harmless.

---

## 8. Architecture Decision Records

Short, dated, and reversible-with-consequences. These are the decisions someone
will question in six months.

**ADR-001 · Event transport is outbox + Django Q2 + HTTP, not a broker**
*Status: Accepted.* No Redis/Kafka available; Q2's ORM broker cannot cross a
database boundary. Consequence: fan-out is O(subscribers) HTTP calls; we own
backoff and DLQ ourselves. Revisit if event volume exceeds ~500/s or if a broker
becomes available — the publisher/subscriber API in `platform_common` is designed
so swapping the transport touches one module.

**ADR-002 · `ledger-svc` is a separate service with exclusive write access to balances**
*Status: Accepted.* Enforces I1 with database credentials rather than
convention. Consequence: +5 ms on the money path, and a saga instead of a local
transaction. Fallback if the team is time-constrained: fold the ledger into
payments-svc as a module with the same API surface; the boundary stays in the
code even if it stops being a process.

**ADR-003 · Funding and transfers are one `payments-svc`**
*Status: Accepted.* They share a lifecycle, a state machine, an idempotency
store and a saga engine; splitting them duplicates all four. Consequence: one
service owns more endpoints than its peers — acceptable, since they are the
same endpoints in different clothes.

**ADR-004 · Eligibility is a module inside `onboarding-svc`, not a service**
*Status: Accepted.* Fails boundary tests (a), (b) and (c): it owns no durable
state of its own, always changes with onboarding product rules, and runs in the
same request cycle. Consequence: if eligibility later grows external
integrations (bureau scores, credit data), it splits out — the seam is already
a `services.py` interface with a typed request/response, so extraction is
mechanical.

**ADR-005 · Fraud fails to REVIEW, never to ALLOW**
*Status: Accepted.* I7. A timeout, a 5xx, or a circuit-breaker trip parks the
transaction in `UNDER_REVIEW` with the hold intact and raises an ops alert.
Consequence: a fraud-svc outage converts into an analyst backlog rather than
fraud losses. A configurable `safe_harbour_amount` may auto-allow small
transfers during an outage — off by default, and an explicit risk acceptance.

**ADR-006 · Each service owns a separate database; SQLite by default**
*Status: Accepted, revised.* Database-per-service isolation with **no database
server at all**: one SQLite file per service under `.data/`. There is not even a
shared process to reach across, so cross-service joins are impossible rather
than merely discouraged. `DB_ENGINE=postgres` switches the whole estate to
PostgreSQL (ten databases, ten roles, `REVOKE CONNECT`) without a code change.
Consequences, both real:
(a) SQLite has no roles, so the DB-enforced append-only guarantee on ledger
postings and the audit log is available only on PostgreSQL — on SQLite it is
application-level. (b) SQLite ignores `SELECT … FOR UPDATE`; serialisation
comes from `transaction_mode=IMMEDIATE` + WAL + a busy timeout instead. The
concurrency tests pass on both, which is the evidence that matters.

**ADR-010 · Money is stored as exact integer minor units, not `DecimalField`**
*Status: Accepted.* Django maps `DecimalField` to a REAL (float) column on
SQLite. Measured: `99999999999999.9999` reads back as `100000000000000.0000`,
and `SUM()` silently drops the fraction — precisely the float-in-a-money-path
defect this design exists to prevent. `platform_common.db.MoneyField` stores a
`BigInteger` count of 1/10,000 units and presents `Decimal`. Consequence: exact
on both backends, `SUM()` becomes integer addition, and the ledger's
debits-equal-credits check is decidable rather than approximate. The ceiling is
±922 trillion (int64 ÷ 10⁴), asserted rather than discovered. `F()` arithmetic
operates on minor units — the ledger reads, computes in `Decimal`, and writes
back inside the locked transaction anyway, because it needs `balance_after`.

**ADR-007 · Realtime UI is polling, not WebSockets**
*Status: Accepted.* Django Channels needs a channel layer; the in-memory layer
is single-process only and there is no Redis. TanStack Query polling with
`?since=` cursors delivers 2–5 s freshness for notifications and ops dashboards
at trivial cost. Consequence: not suitable for a true trading-style UI; fine
for banking.

**ADR-008 · The API gateway is Nginx (config only), with a Python fallback**
*Status: Accepted.* Nginx is a reverse proxy configured by a file, not a
library the team writes code against, and a Django deployment has a web server in
front of it regardless. If company policy forbids it, `gateway-svc` (Django +
`httpx`) is a drop-in at a cost of 2–5 ms per hop and one more process to scale.
Local development uses Vite's dev proxy and no gateway at all.

**ADR-009 · Fraud is rule-based first; ML is an optional pluggable layer**
*Status: Accepted.* The requirement specifies a rule-based engine, and rules are
explainable, tunable by admins, and testable — all of which a model is not.
Consequence: `Scorer` is an interface; an unsupervised anomaly model
(IsolationForest, trained offline, loaded once, ~1 ms) can be added behind it
without touching the decision path. See [`../../wqo.md`](../../wqo.md) for the
options analysis.

---

## 9. What we deliberately did **not** build

Saying no is part of the design. Each of these has a trigger that would change our mind.

| Not building | Why not | Trigger to revisit |
|---|---|---|
| Kafka / event streaming | 12 TPS average doesn't need a log | >500 events/s, or replay-from-genesis becomes a requirement |
| Redis feature store | Postgres meets the 50 ms budget with 11 ms to spare | Fraud p99 > 35 ms |
| Service mesh / mTLS everywhere | 10 services in one trust zone; service JWTs suffice | Multi-tenant or multi-cluster deployment |
| Saga orchestration framework | One saga, five steps — a state machine table is clearer | A third saga appears |
| Separate read databases / full CQRS | Read models inside each service are enough | Reporting starts affecting write latency |
| Kubernetes on day one | docker-compose runs the whole estate; manifests are mechanical later | Multiple environments or autoscaling needed |
| GraphQL / BFF layer | Four role-based UIs, all thin; REST + OpenAPI is the contract | Mobile clients with divergent payload needs |

---

## 10. Reading order

1. **This document** — why the boxes are where they are.
2. [`01-hld.md`](01-hld.md) — the boxes, the topology, cross-cutting concerns, deployment.
3. [`02-lld.md`](02-lld.md) — models, sequences, state machines, algorithms.
4. [`03-events.md`](03-events.md) — the event catalogue and the Django Q2 machinery.
5. [`04-api-contracts.md`](04-api-contracts.md) — the REST surface.
6. [`05-delivery-plan.md`](05-delivery-plan.md) — who builds what, in what order, and how we prove it works.
