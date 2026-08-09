# server — banking platform backend

Ten Django microservices, database-per-service, **Django Q2** as the message
queue. No Redis, no Kafka, no broker, **no database server** — SQLite by
default, one file per service.

**Nothing to install but Python.** PostgreSQL is a one-env-var switch
(`DB_ENGINE=postgres`) when you want it; the code is identical on both.

Design docs: [`../docs/microservices/`](../docs/microservices/README.md)

---

## Requirements

You need **one** thing installed: Python 3.12 or newer. Everything else comes
from the setup script.

| | | |
|---|---|---|
| **Python** | **3.12+** | `python3 --version` |
| Package manager | `uv` *(recommended)* or `pip` | either works — see below |
| Database | **none** | SQLite ships with Python |
| Docker | **not required** | services run as plain processes |
| Redis / Kafka | **not required** | Django Q2 uses the service's own database |
| Disk | ~350 MB | virtualenv + SQLite files |
| RAM | ~1.5 GB | for all 20 processes; ~500 MB for `--core` |
| Ports | 8001–8010 | one per service, loopback only |

Works on macOS, Linux and Windows (WSL recommended on Windows).

<details>
<summary><b>Installing Python 3.12</b></summary>

```bash
# macOS
brew install python@3.12

# Ubuntu / Debian
sudo apt install python3.12 python3.12-venv

# any platform, via uv (no system Python needed)
uv python install 3.12
```

3.12 is declared in `pyproject.toml` (`requires-python = ">=3.12"`), so both
`uv` and `pip` refuse to install `platform_common` on anything older. The
modules themselves happen to import on 3.11, but nothing is tested there — the
whole suite runs on 3.12, so treat older versions as unsupported rather than
broken.
</details>

---

## Quickstart

### With `uv` (recommended)

```bash
# install uv, if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh     # macOS / Linux
# or: brew install uv   |   pipx install uv   |   pip install uv

cd server
uv run python scripts/setup.py             # deps, databases, migrations, seed, schedules
uv run python scripts/run_service.py --all
```

`uv` creates the virtualenv and fetches Python 3.12 itself, so you don't manage
either. `setup.py` is idempotent — re-run it any time.

### With plain `pip`

No `uv` on the machine, or your company image won't allow it? The pinned
dependency lists are committed, so `pip` works identically:

```bash
cd server
python3.12 -m venv .venv
source .venv/bin/activate                  # Windows: .venv\Scripts\activate

pip install -r requirements.txt            # runtime only
# or, to run the test suite too:
pip install -r requirements-dev.txt

python scripts/setup.py
python scripts/run_service.py --all
```

Both requirements files include `-e ./libs/platform_common`, the shared library
every service imports, so **run pip from the `server/` directory** — that
relative path is resolved from your working directory.

| File | Contents |
|---|---|
| `requirements.txt` | 35 pinned runtime packages |
| `requirements-dev.txt` | the above plus pytest, pytest-django, coverage |

Both are generated from `uv.lock`, so the two paths install exactly the same
versions:

```bash
uv export --no-hashes --no-emit-project --no-dev -o requirements.txt
uv export --no-hashes --no-emit-project          -o requirements-dev.txt
```

> The rest of this README writes `uv run python …`. On the pip path, drop the
> `uv run` and use `python …` with the virtualenv activated.

### Check it worked

```bash
curl -s localhost:8006/readyz     # {"status": "ready", "service": "ledger", ...}
uv run python scripts/test_all.py # ~370 tests, 10 suites
```

---

## What setup.py actually does

Six steps, in order. Run them by hand if you prefer:

```bash
uv sync                                                 # 1. deps + platform_common (editable)
uv run python scripts/init_databases.py                 # 2. 10 SQLite files under .data/
uv run python scripts/manage_all.py migrate             # 3. migrate all 10 services
cd services/fraud && uv run python manage.py seed_rules --activate && cd ../..   # 4. fraud rules
uv run python scripts/manage_all.py register_schedules  # 5. Q2 schedules
uv run python scripts/manage_all.py check               # 6. sanity check
```

**Steps 4 and 5 are not optional**, and skipping either produces a system that
looks healthy and isn't:

- without `seed_rules --activate`, every fraud rule sits in `SHADOW` mode — it
  is evaluated and recorded but contributes nothing to the decision, so
  **nothing is ever blocked**;
- without `register_schedules`, no sweeper runs — a lost event is never retried,
  a stranded hold is never released, and the ops dashboard stays empty.

Useful flags:

```bash
uv run python scripts/setup.py --reset     # wipe the databases and start clean
uv run python scripts/setup.py --shadow    # leave fraud rules in SHADOW mode
```

### On PostgreSQL instead

```bash
export DB_ENGINE=postgres
uv run python scripts/setup.py
uv run python scripts/init_databases.py --with-roles      # one role per database
uv run python scripts/init_databases.py --lock-immutable  # after migrating
```

`--with-roles` reproduces the production grant model: each role connects to
exactly one database, `REVOKE CONNECT` from `PUBLIC`, and `UPDATE`/`DELETE`
revoked on the ledger postings and the audit log — which turns "append-only"
into something verifiable with `\dp`. **SQLite has no role system**, so on
SQLite those two guarantees are application-level only. Worth knowing which
you're running.

---

## Running

```bash
uv run python scripts/run_service.py --all      # all ten (20 processes)
uv run python scripts/run_service.py --core     # the UC2 demo path (7 services)
uv run python scripts/run_service.py payments ledger fraud
uv run python scripts/run_service.py audit --no-worker
```

Each service is two processes — `runserver` for the API and `manage.py qcluster`
for the workers — because they scale on different signals in production. Logs
land in `.logs/<service>-<web|worker>.log`. Ctrl-C stops everything.

| Service | Port | Public API |
|---|---|---|
| identity | 8001 | `/api/auth/*`, `/.well-known/jwks.json` |
| onboarding | 8002 | `/api/onboarding/*` |
| kyc | 8003 | *internal only* |
| account | 8004 | `/api/accounts/*`, `/api/beneficiaries/*` |
| payments | 8005 | `/api/funding/*`, `/api/transfers/*` |
| **ledger** | 8006 | **none — internal only, by design** |
| fraud | 8007 | `/api/fraud/*` |
| notification | 8008 | `/api/notifications/*` |
| audit | 8009 | `/api/audit/*` |
| ops | 8010 | `/api/ops/*` |

Every service also serves `/healthz`, `/readyz`, `/internal/metrics`,
`/api/schema` and `/api/docs` (Swagger).

> **A demo needs its whole chain running.** A subscriber that is down doesn't
> lose events — they accumulate as `PENDING` outbox rows on the publisher and
> deliver on recovery — but the workflow won't *advance* until it's up. If an
> application never leaves `KYC_PENDING`, onboarding-svc isn't running to
> receive `kyc.completed`.

---

## Testing

```bash
uv run python scripts/test_all.py                  # every suite (~370 tests)
uv run python scripts/test_all.py --only ledger
uv run python scripts/test_all.py -k concurrency
```

Tests run with `Q_CLUSTER["sync"]=True`, so tasks execute inline and no worker is
needed. Concurrency tests use `transaction=True` and real threads so they
genuinely contend for the write lock.

Test databases are separate files under `.data/test_*.sqlite3` and are
**file-based, not Django's default in-memory one**: the in-memory shared-cache
database takes table-level locks that bypass `busy_timeout`, so concurrent
writers fail instantly with "database table is locked" instead of queueing as
they would in production.

### Proving it works

Three scripts, each a gate rather than a smoke test.

```bash
# P0 - the event backbone
uv run python scripts/run_service.py audit &
uv run python scripts/verify_backbone.py
```
Publishes from payments-svc and asserts the event lands in audit-svc's hash
chain, having crossed `outbox → Q2 → HTTP → inbox → Q2 → handler`.

```bash
# P2 - the money path, across four services over real HTTP
uv run python scripts/run_service.py account ledger fraud audit --no-worker &
uv run python scripts/demo_p2.py
```
Funds an account; a clean transfer settles; a blacklisted payee is hard-blocked
with the balance provably unchanged; a reviewed transfer holds the funds until
an analyst approves and the saga resumes; fraud-svc going dark parks the
transfer under review rather than approving it; the ledger invariants still hold.

```bash
# UC1 - onboarding
uv run python scripts/run_service.py kyc onboarding account audit notification
```
A clean customer reaches `ACCOUNT_OPENED` with a tier and risk score; the seeded
name *Viktor Petrov* lands `KYC_FAILED` on a sanctions hit; *Maria Santos* lands
`MANUAL_REVIEW` as a PEP. Simulated providers are deterministic by input hash,
so these repeat exactly.

---

## Layout

```
server/
├── libs/platform_common/       shared library, installed into every service
│   └── platform_common/
│       ├── events/             envelope · outbox · relay · inbox · dispatcher
│       ├── auth/               JWT (RS256 users, HS256 service tokens), permissions
│       ├── http/               ServiceClient: timeouts, retries, circuit breaker
│       ├── observability/      correlation id · JSON logs · health · metrics
│       ├── db/fields.py        MoneyField (exact integer minor units)
│       ├── money.py            Money value object (Decimal, never float)
│       ├── errors.py           the one error envelope
│       └── service_settings.py base settings + event subscription table
├── services/<name>/            10 Django projects, identical skeleton
│   ├── manage.py · config/{settings,urls,wsgi}.py
│   └── <name>/{models,serializers,views,services,tasks,handlers,clients,urls}.py
├── scripts/                    setup · run · test · migrate · demos
├── requirements.txt            pinned runtime deps (pip path)
├── requirements-dev.txt        + pytest (pip path)
├── pyproject.toml / uv.lock    the source of truth for both
└── .data/                      SQLite files (gitignored)
```

Each service module has a fixed job — deviating is a review finding:

| File | Job |
|---|---|
| `models.py` | ORM models. Never imported by another service. |
| `serializers.py` | **All** input validation. |
| `views.py` | Authorise, parse, delegate, respond. No business logic. |
| `services.py` | Domain logic. The only module that opens `transaction.atomic()`. |
| `tasks.py` | Q2 entrypoints. Thin and idempotent. |
| `handlers.py` | Inbound event handlers. Idempotent and order-tolerant. |
| `clients.py` | Outbound calls. The only module that knows a peer's URL. |

### What each service owns

| Service | Owns | Notable |
|---|---|---|
| identity | users, credentials, RS256 keys, devices | refresh rotation with reuse detection |
| onboarding | applications, eligibility results | state machine + eligibility module (ADR-004) |
| kyc | KYC cases, documents, screening hits | **all PII; none of it leaves in events** |
| account | accounts, beneficiaries, limits | limit *reservations*, not just reads |
| payments | funding, transfers, schedules, saga | 5-step saga, a compensation per step |
| **ledger** | journal, postings, balances, holds | single writer of money; no public route |
| fraud | rules, decisions, cases, feature model | no-`eval` DSL, shadow mode, p99 < 50 ms |
| notification | messages, preferences | dedupe per (event, channel, user) |
| audit | append-only hash-chained log | subscribes to `*` |
| ops | health snapshots, failure cases, reports | snapshots survive a service outage |

---

## How the event backbone works

Django Q2 is a *task queue*, not a broker: no topics, no fan-out, and — with
database-per-service — no way to enqueue into another service's queue. So events
cross service boundaries over HTTP, and Q2's job is making each side durable.

```
publisher                                  subscriber
─────────                                  ──────────
business txn ─┬─ state change               POST /internal/events  (HMAC-signed)
  (ATOMIC)    └─ OutboxEvent row                   │
                    │                              ├─ InboxEvent (unique event_id)
       transaction.on_commit                       └─ 202 Accepted
                    ↓                                    │
            async_task(relay_one)              transaction.on_commit
                    ↓                                    ↓
       Q2 worker: HTTP POST ──────────────▶      async_task(dispatch_one)
                    ↑                                    ↓
       Schedule(1 min) sweeper                   Q2 worker: handlers
       (safety net + backoff)
```

| Guarantee | Mechanism |
|---|---|
| No lost events | Outbox row commits with the state change; the sweeper guarantees eventual send |
| No phantom events | Nothing is published unless the business transaction committed |
| At-least-once delivery | Retry ladder `1s → 2s → 5s → 15s → 60s → 5m → 15m → 1h` then `DEAD` |
| Effect-once processing | `InboxEvent.event_id` is the primary key; a duplicate POST returns 200 and does nothing |
| Publisher never blocked | The subscriber returns 202 before running any handler |
| Out-of-order tolerance | `guard_sequence()` skips events that would move a projection backwards |
| Poison containment | `DEAD` outbox rows and `FAILED` inbox rows surface in ops-svc for replay |

**One outbox row per (event, subscriber)** — a subscriber being down stalls only
its own row.

### Django Q2 settings that are not optional

Verified against django-q2 1.10.0 in the P0 spike:

| Setting | Why |
|---|---|
| `retry > timeout` | Otherwise a task is redelivered while the first copy is still running. django-q2 warns about this itself. |
| `max_attempts` set explicitly | **The default of `0` means infinite.** A poison task would occupy the queue forever. |
| `catch_up: False` | After downtime the scheduler would otherwise fire every missed run at once. |
| `save: False` on high-volume tasks | Q2 writes a result row per execution by default; the relay would generate millions nobody reads. |

The ORM broker claims tasks by compare-and-swap on a `lock` timestamp (not
`SELECT FOR UPDATE SKIP LOCKED`), which is safe for N workers — verified at 12
concurrent workers with zero double-execution — but gives **no FIFO ordering**.
Handlers are order-tolerant by design, so that is fine.

---

## Two things SQLite changes

**Money is not stored as `DecimalField`.** Django maps it to a REAL (float)
column on SQLite. Measured: `99999999999999.9999` reads back as
`100000000000000.0000`, and `SUM()` drops the fraction entirely. All monetary
columns use `platform_common.db.MoneyField`, which stores exact integer minor
units at 4dp and presents `Decimal`. Exact on both backends, and `SUM()` becomes
integer addition. **Never use `DecimalField` for money here.**

**`SELECT ... FOR UPDATE` is a no-op on SQLite.** Row locks are what stop two
concurrent transfers spending the same balance. SQLite has none, so
serialisation comes from `transaction_mode=IMMEDIATE` — the write lock is taken
when the transaction opens rather than on first write — plus WAL and a 30 s busy
timeout. Verified: 20 processes under load, zero lock errors. The concurrency
tests pass identically on both backends, which is the evidence that matters.

---

## Non-negotiable rules

1. **Lock ordering.** Code locking more than one `Balance` sorts by
   `ledger_account_id` ascending. The only reliable deadlock prevention.
2. **No I/O inside `transaction.atomic()`.** Use `transaction.on_commit`.
3. **Every Q2 task is idempotent.** Assume it runs twice.
4. **Money is `Decimal`, 4dp, serialised as a string.** `float` is banned —
   `Money` and `MoneyField` both raise on it.
5. **Cross-service references are UUIDs, never FKs.** A FK across a service
   boundary is a merged service pretending not to be.
6. **`publish()` only inside the transaction that made the change.** It asserts
   on this.
7. **Never raise out of a block whose side effects must survive.** Four separate
   bugs here were exactly that — bookkeeping, a token revocation, or an event
   written inside an `atomic()` that then re-raised, rolling the write back.
   Commit first, raise after.
8. **Retry only what is safe to retry.** `ServiceClient` refuses to retry a
   non-idempotent call after a timeout without an `Idempotency-Key`.
9. **Role and ownership are separate checks.** A valid `CUSTOMER` token is not
   authorisation to touch account X.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Application stuck at `KYC_PENDING` | onboarding-svc isn't running, so `kyc.completed` has nowhere to land. Start the whole chain. |
| Fraud never blocks anything | Rules are in `SHADOW`. Run `seed_rules --activate`. |
| `outbox_pending` climbing in `/internal/metrics` | The subscriber is down. Events are safe and will deliver on recovery. |
| Sweepers never run | `register_schedules` wasn't run. Check with `manage.py register_schedules --list`. |
| `no such table: pc_outbox_event` | Migrations weren't applied for that service. |
| Port already in use | A previous run is still up: `pkill -f run_service.py` |
| Want a clean slate | `uv run python scripts/setup.py --reset` |
| `ModuleNotFoundError: platform_common` | The shared library isn't installed. `uv sync`, or on the pip path `pip install -e ./libs/platform_common` from `server/`. |
| `SyntaxError` on startup | Python is older than 3.12. Check with `python3 --version`. |
| `pip install -r requirements.txt` can't find `./libs/platform_common` | pip was run from the wrong directory — it must be `server/`. |

```bash
tail -f .logs/payments-worker.log                       # follow a worker
curl -s localhost:8005/internal/metrics | python3 -m json.tool
curl -s localhost:8006/readyz
```

Every log line and event carries a `correlation_id` — one id reconstructs a
whole transfer across all ten services.
