# IND Bank Payments Platform — Handbook

**The single document.** If you read one thing before touching this codebase, read this.
It explains what we built, how the data is shaped, how money actually moves, how we
prove it works, and — for each significant choice — *why we chose it and what we would
say when someone pushes back*.

Deeper dives live in [`docs/microservices/`](microservices/README.md). This document is
the map; those are the territory.

---

## Table of contents

1. [What this is](#1-what-this-is)
2. [Run it in five minutes](#2-run-it-in-five-minutes)
3. [The shape of the system](#3-the-shape-of-the-system)
4. [Every table we have](#4-every-table-we-have)
5. [How services talk to each other](#5-how-services-talk-to-each-other)
6. [The money path — the saga](#6-the-money-path--the-saga)
7. [The ledger](#7-the-ledger)
8. [The fraud engine](#8-the-fraud-engine)
9. [Authentication and authorisation](#9-authentication-and-authorisation)
10. [The frontend](#10-the-frontend)
11. [How we verify everything](#11-how-we-verify-everything)
12. [Defending this in a system design interview](#12-defending-this-in-a-system-design-interview)
13. [Edge cases — what breaks, and what we do](#13-edge-cases--what-breaks-and-what-we-do)
14. [What is not done](#14-what-is-not-done)

---

## 1. What this is

Two banking use cases, joined together so the second depends on the first:

**Use case 1 — Digital onboarding.** A person registers, submits their details, passes a
KYC check, gets scored for eligibility, and an account is opened for them with an account
number and a tier.

**Use case 2 — Funding and transfers with real-time fraud screening.** That customer puts
money into the account, then sends money out — inside IND Bank, across India, or
internationally. Every outgoing payment is scored by a fraud engine before it is allowed
to leave, and a held payment goes to a human analyst.

Around those two flows sit the things a real bank needs: a double-entry ledger, an audit
trail that cannot be quietly edited, an operations console, and notifications.

**Nothing here moves real money.** The payment rails are simulated. The KYC provider is
simulated. Everything else — the ledger, the saga, the fraud rules, the audit chain — is
the real logic.

### The numbers

| | |
|---|---|
| Backend services | 10 Django projects |
| Shared library | 1 (`platform_common`) |
| Databases | 10 SQLite files, one per service |
| Tables | 46 (43 business + 3 platform tables, replicated per service) |
| Event types | 37 |
| Fraud rules | 15 |
| Backend tests | 374, all passing |
| Frontend | 1 Vite + React SPA, 22 screens, 4 role consoles |
| Python | ~19,600 lines |
| Frontend | ~8,300 lines |

---

## 2. Run it in five minutes

You need Python 3.12 and Node 20+. Nothing else — no Docker, no Postgres, no Redis.

```bash
# --- backend ---
cd server
uv sync                          # or: python -m venv .venv && .venv/bin/pip install -e .
uv run python scripts/setup.py   # creates 10 DBs, migrates, seeds rules + demo users
uv run python scripts/run_service.py --all      # all 10 services + their Q2 workers

# --- frontend, in a second terminal ---
cd client
npm install
npm run dev                      # http://localhost:5173
```

Demo logins (all use password `demo-password-2026`):

| Email | Role | Console |
|---|---|---|
| `asha@indbank.test` | Customer | Accounts, send, add money, payees, schedules |
| `analyst@indbank.test` | Fraud analyst | Case queue, case detail, rule performance |
| `ops@indbank.test` | Operations | Service health, failures, reports |
| `admin@indbank.test` | Administrator | Rules, thresholds, limits, audit trail |

To get realistic data in the system, run `uv run python scripts/seed_demo_data.py`.

> **Why staff users are seeded rather than registered.** The public `/api/auth/register`
> endpoint always creates a `CUSTOMER`. If it accepted a `role` field, anyone could sign
> up as an admin. So staff accounts can only be created by a management command with
> filesystem access. This is a real security boundary, not a convenience.

---

## 3. The shape of the system

Ten services. Each one owns its own database and its own port. **No service can read
another service's tables** — not "shouldn't", *can't*, because they are separate SQLite
files with separate connections.

| Port | Service | Owns | Talks to |
|---|---|---|---|
| 8001 | `identity` | Users, passwords, devices, JWT signing keys, refresh tokens | — |
| 8002 | `onboarding` | Applications, eligibility scoring, the onboarding state machine | kyc, account |
| 8003 | `kyc` | KYC cases, identity + document checks, sanctions screening | — |
| 8004 | `account` | Accounts, beneficiaries, limit policies, limit usage | ledger |
| 8005 | `payments` | Transactions, the saga, idempotency, schedules, funding sources | account, ledger, fraud |
| 8006 | `ledger` | Chart of accounts, journal entries, postings, holds, balances | — |
| 8007 | `fraud` | Rules, thresholds, decisions, cases, account profiles | — |
| 8008 | `notification` | Notifications, delivery, preferences | — |
| 8009 | `audit` | The hash-chained append-only log | — |
| 8010 | `ops` | Health snapshots, failure cases, reports | all (health polling) |

Two things worth noticing:

- **`ledger` and `fraud` call nobody.** They are pure: you give them a request, they give
  you an answer. That makes them trivially testable and impossible to deadlock.
- **`payments` is the only orchestrator.** It is the one service that coordinates others,
  and it does so through an explicit saga with explicit compensation. Everything else is
  either a leaf or a simple state machine.

### Two ways services communicate

**Synchronous (HTTP, `/internal/*`)** — used when the caller cannot continue without the
answer. "Is this transfer within limits?" "Place a hold." "Score this payment." These are
blocking calls, authenticated with a shared-secret HMAC token, and they are on the money
path.

**Asynchronous (events)** — used when the caller does not need an answer. "A payment
settled." "An account was opened." These go through a transactional outbox, get delivered
over signed HTTP, and land in the subscriber's inbox. Nothing on the money path waits for
them.

The rule we followed: **if a failure to deliver would corrupt money, it is synchronous. If
it would only delay a notification or a report, it is an event.**

---

## 4. Every table we have

46 tables. Grouped by owner. The three `pc_*` tables exist inside *every* service database
— that is the point of the outbox pattern.

### Platform (in all 10 databases)

**`pc_outbox_event`** — events waiting to be delivered to a subscriber.
`id · event_id · event_type · aggregate_id · subscriber · envelope(JSON) · status · attempts · next_attempt_at · last_error · created_at · sent_at`
Unique on `(event_id, subscriber)` — one row per *subscriber*, so a retry to `notification`
never re-sends to `audit`.

**`pc_inbox_event`** — events received, keyed by the producer's `event_id`.
`event_id(PK) · event_type · aggregate_id · sequence · producer · correlation_id · envelope · status · attempts · last_error · received_at · processed_at`
`event_id` is the primary key, which is what makes redelivery harmless: the second insert
simply fails.

**`pc_projection_cursor`** — `projection · aggregate_id · last_sequence · updated_at`.
Lets a consumer ignore an event that arrives *older* than one it has already applied.

### identity (4 tables)

| Table | Key columns |
|---|---|
| `identity_user` | `id · email(unique) · phone · password_hash · full_name · role · status · mfa_secret · mfa_enabled · failed_logins · locked_until · last_login_at · created_at` |
| `identity_signing_key` | `kid(unique) · public_pem · private_pem · is_active · created_at · retires_at` |
| `identity_device` | `id · user → user · fingerprint_hash · user_agent · last_ip_country · trusted · first_seen · last_seen` — unique on `(user, fingerprint_hash)` |
| `identity_refresh_token` | `jti(PK) · user · device · issued_at · expires_at · revoked_at · replaced_by(1-1) · family_id` |

`replaced_by` and `family_id` together implement refresh-token rotation with theft
detection — see [§9](#9-authentication-and-authorisation).

### onboarding (3 tables)

| Table | Key columns |
|---|---|
| `onboarding_application` | `id · user_id · status · status_reason · customer_info(JSON) · kyc_case_id · account_id · account_number · sequence · correlation_id · created_at · updated_at` |
| `onboarding_status_history` | `application → · from_status · to_status · reason · at` |
| `onboarding_eligibility_result` | `application(1-1) · decision(PASS/REVIEW/FAIL) · risk_score · tier · factors(JSON) · policy_version · created_at` |

`factors` is a per-factor breakdown, not just a number. A customer told "you were declined"
deserves a reason, and a regulator will ask for one.

### kyc (4 tables)

| Table | Key columns |
|---|---|
| `kyc_case` | `id · application_id · user_id · status · identity_score · document_score · sanctions_hit · pep_hit · risk_rating · provider_ref · failure_reason · processing_ms · correlation_id · created_at · completed_at` — unique on `application_id` |
| `kyc_identity` | `case(1-1) · full_name · date_of_birth · national_id · national_id_hash · nationality · address(JSON) · purged_at` |
| `kyc_document` | `id · case → · doc_type · filename · authenticity_score · ocr_result(JSON) · status · uploaded_at` |
| `kyc_screening_hit` | `id · case → · list_name · matched_name · match_score · is_pep · resolved · resolution_note · created_at` |

`purged_at` exists because raw identity data has a retention limit. `national_id_hash` is
indexed so we can still detect the same person applying twice after the raw value is gone.

### account (5 tables)

| Table | Key columns |
|---|---|
| `account_account` | `id · user_id · account_number(unique) · ifsc · currency · account_type · status · tier · opened_at · idempotency_key · balance_as_of` |
| `account_beneficiary` | `id · user_id · nickname · beneficiary_type · account_number · bank_code · swift_bic · country · currency · status · cooling_off_until · fingerprint · created_at` — unique on `(user_id, fingerprint)` |
| `account_limit_policy` | `id · scope(TIER/ACCOUNT) · scope_ref · rail · currency · …max amounts… · daily_count_max · version · updated_by · updated_at` — unique on `(scope, scope_ref, rail)` |
| `account_limit_usage` | `id · account_id · window · rail · …amount used… · count_used` — unique on `(account_id, window, rail)` |
| `account_limit_reservation` | `id · account_id · rail · amount · currency · day_window · month_window · released · created_at` |

`window` is a string like `"DAY:2026-08-08"` or `"MONTH:2026-08"`. Encoding the period into
the unique key means the daily counter resets by *not existing yet* rather than by a
scheduled job that could fail to run.

`account_limit_reservation` is the thing that makes limits correct under concurrency: we
reserve budget *before* screening, and release it if the payment dies.

### payments (5 tables)

| Table | Key columns |
|---|---|
| `payments_transaction` | `id · reference(unique) · user_id · account_id · amount · currency · fx_rate · dest_currency · txn_type · rail · direction · beneficiary_id · beneficiary_masked · funding_source_id · status · status_reason · hold_id · journal_entry_id · fraud_decision_id · fraud_case_id · fraud_score · limit_reservation_id · rail_ref · schedule_id · idempotency_key · correlation_id · sequence · purpose_code · remarks · context(JSON) · created_at · updated_at · settled_at` |
| `payments_saga_step` | `id · transaction → · name · status · attempt · request(JSON) · response(JSON) · error · started_at · finished_at` |
| `payments_idempotency` | `id · user_id · key · endpoint · request_hash · state · status_code · response_body(JSON) · transaction_id · created_at · completed_at` — unique on `(user_id, key, endpoint)` |
| `payments_transfer_schedule` | `id · user_id · account_id · beneficiary_id · amount · currency · rail · remarks · frequency · …dates, run counts…` |
| `payments_funding_source` | `id · user_id · source_type · display_name · token · currency · verified · verification_method · is_active · created_at` |

Note `payments_funding_source.token`: **we never store a card number.** The API actively
rejects anything that looks like a PAN (13–19 digits after stripping spaces and dashes)
and tells you to send the vault token instead.

`payments_saga_step` stores the request and response of every step. When a transfer goes
wrong, the trace is already on disk — nobody has to reproduce it from logs.

### ledger (6 tables)

| Table | Key columns |
|---|---|
| `ledger_account` | `id · code(unique) · account_ref · kind · currency · normal_side · is_active · created_at` |
| `ledger_balance` | `ledger_account(1-1) · …available/held/total… · version · updated_at` |
| `ledger_journal_entry` | `id · reference(unique) · entry_type · txn_ref · idempotency_key(unique) · reverses → · correlation_id · narrative · posted_at · created_at` |
| `ledger_posting` | `id · journal_entry → · ledger_account → · direction · amount · currency · created_at` |
| `ledger_hold` | `id · ledger_account → · amount · currency · txn_ref · idempotency_key(unique) · status · expires_at · captured_by → · created_at · resolved_at` |
| `ledger_invariant_check` | `checked_at · check_name · currency · ok · detail(JSON)` |

`ledger_balance.version` is an optimistic-locking counter. `ledger_invariant_check` is a
scheduled job writing down whether the books still balance — see [§7](#7-the-ledger).

### fraud (9 tables)

| Table | Key columns |
|---|---|
| `fraud_rule` | `id · code(unique) · name · description · condition(JSON) · weight · hard_block · mode · reason_code · category · version · created_by · updated_by · created_at · updated_at` |
| `fraud_rule_stat` | `rule(1-1) · fired_count · shadow_fired_count · confirmed_fraud · false_positive · updated_at` |
| `fraud_threshold` | `allow_below · block_at_or_above · version · is_active · updated_by · updated_at` |
| `fraud_decision` | `id · txn_ref(unique) · account_ref · user_ref · decision · score · reason_codes(JSON) · shadow_codes(JSON) · features(JSON) · amount · currency · rail · ruleset_version · latency_ms · correlation_id · created_at` |
| `fraud_case` | `id · decision(1-1) · txn_ref · account_ref · status · priority · resolution · assigned_to · sla_due_at · resolution_note · resolved_by · resolved_at · created_at` |
| `fraud_account_profile` | `account_ref(PK) · txn_count · rolling mean/variance · last_country · last_device_hash · last_txn_at · first_seen_at · account_opened_at · updated_at` |
| `fraud_screened_txn` | `txn_ref(PK) · account_ref · currency · rail · benef_fingerprint · country · device_hash · decision · created_at` |
| `fraud_known_beneficiary` | `account_ref · fingerprint · first_seen · txn_count` |
| `fraud_list_entry` | `id · list_type · value · reason · added_by · is_active · created_at` |

`fraud_decision.features` records the exact inputs the engine saw. Six months later, when
someone asks "why was this blocked?", we do not have to guess — the answer is stored.

`fraud_screened_txn` is a deliberately small rolling copy of recent traffic, kept only so
velocity rules can count. It is pruned on a schedule. It is *not* a second copy of the
payments database.

### notification (2 tables)

| Table | Key columns |
|---|---|
| `notification_notification` | `id · user_id · channel · template_code · subject · body · status · attempts · last_error · source_event_id · correlation_id · read_at · created_at · sent_at` |
| `notification_preference` | `user_id · category · channel · enabled` — unique on `(user_id, category, channel)` |

`source_event_id` is part of a unique constraint. If the same event is delivered twice, the
customer still gets exactly one message.

### audit (2 tables)

| Table | Key columns |
|---|---|
| `audit_log` | `id(BigAuto) · event_id(unique) · event_type · event_version · occurred_at · recorded_at · producer · actor_type · actor_id · aggregate_type · aggregate_id · sequence · correlation_id · causation_id · payload(JSON) · prev_hash · row_hash` |
| `audit_chain_verification` | `started_at · finished_at · from_id · to_id · rows_checked · ok · broken_at_id · detail` |

`row_hash = sha256(prev_hash + the row's own content)`. Change any historical row and every
hash after it stops matching. See [§13](#13-edge-cases--what-breaks-and-what-we-do) for the
honest limits of that claim.

### ops (3 tables)

| Table | Key columns |
|---|---|
| `ops_health_snapshot` | `service · captured_at · reachable · queue_depth · outbox_pending · outbox_dead · inbox_failed · oldest_pending_age_s · extra(JSON) · error` |
| `ops_failure_case` | `id · failure_type · source_service · subject_ref · correlation_id · status · detail(JSON) · assigned_to · resolution_note · opened_at · resolved_at` — unique on `(failure_type, subject_ref)` |
| `ops_report` | `id · report_type · params(JSON) · status · result(JSON) · rows · requested_by · created_at · completed_at` |

That unique constraint on `ops_failure_case` matters: a failure that keeps re-firing opens
**one** case, not one per retry. Ops teams drown in duplicate alerts; this prevents it.

---

## 5. How services talk to each other

### The transactional outbox

The problem: you update your database *and* you need to tell another service. Do them
separately and you eventually get one without the other — you either lose the message or
announce something that did not happen.

Our solution, which is the standard one:

```
BEGIN
  update the business tables
  insert a row into pc_outbox_event      ← same transaction, same database
COMMIT
                                          ← a worker picks it up afterwards
  POST it to the subscriber (signed)
  subscriber writes it to pc_inbox_event
  subscriber processes it
```

Because the business change and the outbox row commit together, they cannot disagree. The
delivery afterwards can fail as often as it likes — the row is still sitting there.

`publish()` actively refuses to run outside a transaction (it raises
`PublishedOutsideTransaction`). That is not a stylistic preference: publishing outside the
transaction silently reintroduces the exact bug the pattern exists to prevent.

### Delivery guarantees

**At-least-once, made idempotent by the inbox.** `pc_inbox_event.event_id` is the primary
key, so a duplicate delivery is a failed insert, which we treat as success and return
`200`. The producer stops retrying. Nothing is processed twice.

Transport is a signed `POST` — `X-Signature` (HMAC) plus `X-Signature-Timestamp`, so a
replayed request outside the time window is rejected.

Retries use exponential backoff via `next_attempt_at`. After the attempt limit the row goes
to a dead state and emits `outbox.dead`, which `ops` subscribes to. **A permanently
undeliverable event becomes a visible failure case, not a silent gap.**

### The event catalogue — 37 types

`"*": ["audit"]` is the first line of the routing table: **audit subscribes to everything.**

| Producer | Events | Who listens |
|---|---|---|
| identity | `user.registered` | notification, onboarding |
| | `user.logged_in` | fraud |
| | `user.login_failed` | notification, ops, fraud |
| | `device.seen` | fraud |
| | `security.refresh_reuse_detected` | notification, ops, fraud |
| onboarding | `onboarding.status_changed` | notification |
| | `onboarding.completed` | notification, account |
| | `onboarding.rejected` | notification, ops |
| | `onboarding.review_required` | ops |
| kyc | `kyc.completed` | notification, onboarding, fraud |
| | `kyc.screening_hit` | ops, onboarding, fraud |
| account | `account.opened` | notification, fraud, onboarding, identity |
| | `account.frozen` | notification, ops, payments, fraud |
| | `beneficiary.added` | notification, fraud |
| | `beneficiary.blocked` | notification, fraud |
| | `limit.policy_updated` | ops, payments |
| payments | `payment.initiated` | ops |
| | `payment.approved` | notification, ops, account |
| | `payment.blocked` | notification, ops, account |
| | `payment.review_required` | notification, ops |
| | `payment.dispatched` | ops |
| | `payment.settled` | notification, ops, account, fraud |
| | `payment.returned` | notification, ops, account, fraud |
| | `payment.failed` | notification, ops, account |
| | `payment.cancelled` | notification, ops, account |
| | `schedule.created` | notification |
| | `schedule.failed` | notification, ops |
| ledger | `ledger.posted` | payments, account |
| | `ledger.reversed` | notification, ops, payments, account |
| | `ledger.invariant_breached` | ops |
| fraud | `fraud.decision_made` | ops |
| | `fraud.case_opened` | notification, ops |
| | `fraud.case_approved` | notification, ops, payments |
| | `fraud.case_rejected` | notification, ops, payments |
| | `fraud.rule_updated` | ops |
| platform | `outbox.dead` | ops |
| | `audit.chain_broken` | ops |

The routing table lives in one place (`platform_common/service_settings.py`), so "who hears
about payments settling?" is answered by reading one dictionary rather than grepping ten
services.

### Correlation IDs

Every inbound request gets an `X-Correlation-Id` (generated if absent). It rides along every
internal call and every event. `GET /api/audit/trace/{correlation_id}` reassembles the whole
story across all ten services.

That endpoint orders by `occurred_at`, **not by `id`**. Arrival order is not causal order —
if `notification` is slow, its rows land later but happened earlier. Ordering by insert ID
would show the trace in the wrong sequence, which is worse than showing nothing.

---

## 6. The money path — the saga

There is no distributed transaction. There cannot be — the services have separate databases.
Instead, a transfer is five steps, each with an explicit way to undo it.

```
VALIDATE  ──▶ PLACE_HOLD ──▶ SCREEN ──▶ CAPTURE ──▶ DISPATCH
   │              │                        │           │
undo:          undo:                    undo:       undo:
release        release                  reverse     reverse
limit          hold                     entry       entry
reservation
```

Written out:

| Step | What happens | Calls | If it fails |
|---|---|---|---|
| **VALIDATE** | Is the account open? Is the payee active? Is this within daily/monthly limits? Reserve limit budget. | `account:/internal/validate-transfer` | Nothing to undo — nothing has happened yet |
| **PLACE_HOLD** | Reserve the money on the ledger. Not taken — held. | `ledger:/internal/holds` | Release the limit reservation |
| **SCREEN** | Score the payment against 15 fraud rules. | `fraud:/internal/screen` | Release the hold, release the reservation |
| **CAPTURE** | Convert the hold into real double-entry postings. | `ledger:/internal/holds/{id}/capture` | Release hold + reservation |
| **DISPATCH** | Send the instruction to the payment rail. | (simulated rail) | Reverse the journal entry, release the reservation |

### Why hold-then-capture instead of just moving the money

If you debit at the start and the payment is later blocked, you have to credit it back —
and for a moment the customer's money was gone for a reason that turned out to be wrong.
If you debit at the end, you have no protection against the customer spending the same
balance twice while screening runs.

Holding solves both. The money is **unavailable but not taken**. The customer's available
balance drops immediately, so they cannot double-spend. Their total balance is unchanged, so
nothing was actually removed. If screening blocks the payment, we release the hold and there
is nothing to reverse — no reversal entries in the ledger, no confusing statement line.

### The three outcomes of SCREEN

| Score | Decision | What happens |
|---|---|---|
| below 40 | **ALLOW** | Saga continues straight to CAPTURE |
| 40–74 | **REVIEW** | Transaction becomes `UNDER_REVIEW`, a `fraud_case` opens, **the hold stays in place**. Saga stops. |
| 75 or above | **BLOCK** | Hold released, limit reservation released, transaction `BLOCKED`. Nothing was taken. |

Those numbers come from `fraud_threshold` and an admin can move them at runtime.

When an analyst approves a held case, the saga **resumes at CAPTURE** — it does not start
over. The hold is still there and still valid; re-running VALIDATE would double-reserve the
limit and re-running PLACE_HOLD would hold the money twice.

### When compensation itself fails

This is the case most designs skip. If we are unwinding a failed payment and the *unwind*
call fails, we do not pretend it succeeded and we do not lose the fact. The transaction goes
to `COMPENSATION_PENDING`, the step is marked `COMPENSATION_FAILED`, and
`payments.tasks.retry_compensation` keeps retrying.

**Money is never left in limbo silently.** It may be stuck, but the stuck state is a
first-class status that ops can see and a scheduled job is working on.

### The 17 transaction states

`INITIATED → VALIDATED → RESERVED → SCREENING → APPROVED → POSTED → DISPATCHED → SETTLED`

with the branches: `UNDER_REVIEW`, `BLOCKED`, `REJECTED`, `CANCELLED`, `EXPIRED`, `FAILED`,
`RETURNED`, `REVERSED`, `COMPENSATION_PENDING`.

Six are terminal and never transition again: `SETTLED`, `BLOCKED`, `REJECTED`, `CANCELLED`,
`EXPIRED`, `REVERSED`.

A customer can cancel only from `INITIATED`, `VALIDATED`, `RESERVED`, or `UNDER_REVIEW` —
that is, **only before the instruction has left the building.** Once dispatched, cancelling
is a lie; the correct action is a return or a reversal, which is a different flow.

### Idempotency

Every money-moving `POST` requires an `Idempotency-Key` header. Without one, the request is
rejected with `400` — we do not guess.

`payments_idempotency` is unique on `(user_id, key, endpoint)` and stores a `request_hash`:

- **Same key, same body** → the original response is replayed. No second payment.
- **Same key, different body** → `409 Conflict`. This is the important one. Silently
  replaying the first answer would tell the client "your ₹90,000 transfer succeeded" when
  they actually asked for ₹9,000.
- **Same key, still in flight** → `409`, because the answer does not exist yet.

The frontend mints the key **when the form opens**, not when Send is clicked. A double-click,
a flaky connection, an impatient retry — all carry the same key.

---

## 7. The ledger

Double-entry. Every journal entry has at least two postings and they must sum to zero per
currency. That is enforced in code at write time, not merely hoped for.

### Balances

Three numbers per ledger account, and the distinction is the whole point:

- **available** — what can be spent right now
- **held** — reserved by an in-flight payment
- **total** — available + held

A hold moves money from available to held. A capture removes it from held and posts it out
for real. A release moves it back to available.

### Money is never a float

`MoneyField` stores an **integer of minor units at 4 decimal places**. Not a `DecimalField`
— on SQLite, Django's `DecimalField` round-trips through a float, and floats cannot
represent `0.1`. Money that cannot represent one tenth is not money.

Four decimal places, not two, because FX rates and interest need sub-paisa precision.
Rounding is `ROUND_HALF_EVEN` (banker's rounding), which does not drift upward over millions
of operations the way `ROUND_HALF_UP` does.

Amounts cross the API as **strings**, never JSON numbers. `{"amount": 0.1}` parsed by
JavaScript is `0.1000000000000000055511151231257827`. A string stays exact.

### Concurrency

`ledger_balance.version` is an optimistic lock. Two payments racing on the same account:
both read version 7, both try to write version 8, one wins, the loser retries against the
new state. There is a dedicated test file (`test_concurrency.py`) for this.

### Invariants

`ledger.tasks.verify_invariants` runs on a schedule and asks: does the trial balance sum to
zero? Do the cached balances match the sum of postings? Are there holds outstanding against
accounts that no longer exist?

Results go to `ledger_invariant_check`. A failure emits `ledger.invariant_breached`, which
`ops` subscribes to.

**A ledger that is only checked when someone complains is not checked.**

---

## 8. The fraud engine

### The 15 rules

| Code | Name | Weight | Category |
|---|---|---|---|
| R001 | Amount threshold | 30 | AMOUNT |
| R002 | Velocity — 5 minute count | 40 | VELOCITY |
| R003 | Value velocity | 35 | VELOCITY |
| R004 | Beneficiary fan-out | 50 | VELOCITY |
| R005 | New beneficiary, large amount | 45 | BENEFICIARY |
| R006 | Blacklisted beneficiary | **100 · hard block** | LIST |
| R007 | High-risk corridor | 40 | GEO |
| R008 | Impossible travel | 55 | GEO |
| R009 | New device, large amount | 35 | DEVICE |
| R010 | Blocked device | **100 · hard block** | LIST |
| R011 | Amount anomaly (z-score) | 40 | BEHAVIOUR |
| R012 | Odd hours, large amount | 25 | BEHAVIOUR |
| R013 | First transaction, large | 45 | BEHAVIOUR |
| R014 | International, new payee, high value | 45 | BENEFICIARY |
| R015 | Cooling-off period breach | 50 | BENEFICIARY |

Scores are additive and capped at 100. Two `hard_block` rules jump straight to BLOCK
regardless of the total — a blacklisted payee is not something you weigh against other
factors.

### The rule language

Rules are **JSON, not code**. There is no `eval` anywhere near them.

```json
{"all": [
  {"fact": "rail",               "op": "eq",  "value": "INTERNATIONAL"},
  {"fact": "beneficiary_is_new", "op": "eq",  "value": true},
  {"fact": "amount",             "op": "gt",  "value": "100000"},
  {"any": [
    {"fact": "destination_is_high_risk", "op": "eq", "value": true},
    {"fact": "amount_zscore",            "op": "gt", "value": 3.0}
  ]}
]}
```

The grammar is four node types (`all`, `any`, `not`, and a leaf) and nine whitelisted
operators (`eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `between`). An unknown
operator or an unknown fact is rejected **when the rule is saved**, not when it runs.

This is the difference between "an admin can tune fraud rules" and "an admin has remote code
execution on the fraud service."

### Shadow mode

A rule can be `SHADOW`: it is evaluated, and the fact that it fired is recorded in
`shadow_codes` and `shadow_fired_count` — but it contributes **zero** to the score.

So you can deploy a new rule, watch it for a week against real traffic, see how often it
would have fired and how often those would have been false positives, and only then turn it
on. New rules default to `SHADOW`.

### Dry run

`POST /api/fraud/rules/dry-run` replays a proposed rule against the last 30 days of
`fraud_screened_txn` and reports how many transactions it would have caught. An admin can
see the blast radius of a change before making it.

### Behavioural stats

`fraud_account_profile` keeps a rolling mean and variance per account using **Welford's
algorithm** — a numerically stable one-pass method. We do not re-read a customer's history
on every payment; we update running statistics incrementally. That is what makes the
z-score rule (R011) cheap enough to run inline.

### When fraud is down

**A screening failure produces REVIEW, never ALLOW.** If the fraud service is unreachable,
the payment is held for a human rather than waved through.

This is the single most important decision in the whole fraud design. The tempting
alternative — fail open so customers are not inconvenienced — means **an attacker's best
move is to take down the fraud service.** Failing closed turns an outage into analyst
workload, which is expensive but survivable.

### Case handling

A REVIEW opens a `fraud_case` with a priority and an `sla_due_at`. The analyst queue is
sorted **by SLA, not by score** — the queue's job is to ensure nothing ages out unreviewed,
and a breached case matters more than a high-scoring one with two hours left on the clock.

Approving or rejecting a case updates `fraud_rule_stat.confirmed_fraud` /
`false_positive` for every rule that fired. Over time this shows which rules are earning
their place and which are just generating work.

---

## 9. Authentication and authorisation

### Two completely separate token systems

**Users get RS256 JWTs.** `identity` signs with a private key; every other service verifies
using the public key fetched from `/.well-known/jwks.json` and cached. **No other service
ever holds the private key**, so no other service can mint a token — even if one is fully
compromised.

**Services get HMAC (HS256) tokens** for `/internal/*` calls. Symmetric is fine here because
both ends are ours and we want it fast; these are on the money path.

The split matters: the blast radius of a compromised *service* is bounded, because a service
credential cannot impersonate a *user*.

### Token lifetimes

Access token: **15 minutes**. Refresh token: **7 days**.

### Refresh rotation with theft detection

Every refresh returns a **new** refresh token and marks the old one `replaced_by` the new
one. All tokens from one login share a `family_id`.

If a *already-used* refresh token is presented again, that means two parties hold the same
token — the legitimate user and someone who stole it. We cannot tell which one is asking.
So we **revoke the entire family**, log everyone out, and emit
`security.refresh_reuse_detected` to notification, ops, and fraud.

Forcing a real user to log in again is a small cost. Leaving an attacker with a live session
is not.

### The frontend's part

The access token lives in a **module variable** — not `localStorage`, not a cookie. Any XSS
that can read `localStorage` gets a 7-day session; a module variable dies with the tab.

The refresh token is in `sessionStorage`, which is a deliberate, stated trade-off: it
survives a page reload but not a closed tab.

Concurrent `401`s are **coalesced into one refresh call** — all waiting requests share a
single promise. Without this, five parallel requests hitting an expired token would fire
five refreshes, four of which would present an already-rotated token, and the backend would
correctly interpret that as theft and log the user out.

### Roles

`CUSTOMER`, `FRAUD_ANALYST`, `OPS`, `ADMIN`. Enforced by DRF permission classes on the
backend and used for route guarding on the frontend.

**The frontend guard is convenience, not security.** Every staff endpoint checks the role
server-side. The SPA hiding a link stops an honest user from getting a confusing 403; it
stops an attacker from nothing.

---

## 10. The frontend

One Vite + React SPA (`client/`), JavaScript with JSX. Two added dependencies:
`react-router-dom` and `@tanstack/react-query`.

### 22 screens across 4 consoles

| Console | Screens |
|---|---|
| Public | Home, Login, Register, Onboarding |
| Customer | Accounts, Send money, Add money, Activity, Transaction detail, Payees, Schedules, Inbox |
| Analyst | Case queue, Case detail, Rule performance |
| Ops | Service health, Failures, Reports |
| Admin | Fraud rules, Thresholds, Limits, Audit trail |

### No API gateway — a Vite proxy instead

Ten services on ten ports would normally mean CORS everywhere, or an nginx gateway to
install and configure. Instead, `vite.config.js` proxies by path prefix:

```js
'/api/auth': 8001, '/api/onboarding': 8002, '/api/kyc': 8003,
'/api/accounts': 8004, '/api/transactions': 8005, '/api/ledger': 8006,
'/api/fraud': 8007, '/api/notifications': 8008, '/api/audit': 8009,
'/api/ops': 8010,   // …and so on
```

The browser sees one origin, so **CORS never happens in development**. (CORS support exists
in `platform_common` for when it does — it echoes a specific origin from an allow-list and
never `*`, because `*` with `Allow-Credentials` is a spec violation browsers reject.)

The obvious risk is drift: change a route in Django and the proxy table quietly stops
matching. So `verify_spa_api.py` reads **the same routing table** and exercises every path
over real HTTP. Drift becomes a failing test rather than a blank screen.

### Money in the UI

Amounts are strings from the API to the pixel. `groupIndian()` does Indian digit grouping
(`12,34,567.89`) **on the string** — no `parseFloat` anywhere on the money path.

### Design

"Ledger terminal": zero border radius anywhere (there is deliberately no `--radius` token so
nobody can reach for one), corner ticks instead of rounded corners, hairline structural
grid, hard 2px frames, offset — never blurred — shadows. Instrument Serif for display,
Archivo for UI, IBM Plex Mono for every number.

Each role has an accent colour. Allow-green, review-amber and block-red are load-bearing, so
BLOCK also carries a diagonal hatch — a colourblind analyst must not depend on hue alone to
see that a payment was stopped.

---

## 11. How we verify everything

Five gates, each catching a class the others cannot.

### Gate 1 — unit and integration tests: 374 passing

```
platform_common  90    identity 29    onboarding 22    kyc 12
account          18    payments 58    ledger   50    fraud 61
notification     14    ops      20
```

Run with `uv run python scripts/test_all.py`. Each service is its own Django project with
its own settings, so they cannot share one pytest invocation — the runner drives each with
its own rootdir.

> **A real caveat about this number.** The runner only picks up a service that already has
> `test_*.py` files. `audit` has none, so it is silently skipped and the run still prints
> "all suites passed". The tenth suite in that list is `platform_common`, not `audit`. See
> [§14](#14-what-is-not-done).

### Gate 2 — the event backbone: `verify_backbone.py`

Proves an event genuinely crosses a service boundary: published inside a transaction, sent
over signed HTTP, deduplicated by the inbox, retried on failure, and dead-lettered when it
runs out of attempts.

### Gate 3 — the API contract: `verify_spa_api.py`, 47/47

Runs against the **live estate over real HTTP**, using the same route table the frontend
proxy uses. It does not just check status codes — it checks behaviour:

- a raw card number is refused
- a malformed IFSC is refused at the edge
- balances come back as strings, not floats
- beneficiary responses mask the account number
- replaying an `Idempotency-Key` returns the original, not a second payment
- the same key with a *different* body returns `409`, not a silent replay
- a transfer with no `Idempotency-Key` is refused
- the transaction detail includes the full saga trace

### Gate 4 — the browser: `walkthrough.mjs`

Playwright + Chromium. Signs in as all four roles, visits all 18 authenticated screens, and
fails on any console error, page error, failed request, `4xx`/`5xx` on an API call,
unexpected redirect, or a page that renders under 40 characters.

**This gate exists because of a mistake.** The first time the work was reported "done", the
UI had never been opened. It built cleanly and linted cleanly. Opening a browser found five
real defects, including a `navigate` reference that was out of scope and would have crashed
the send-money page on click. An API-level gate cannot catch a React app that renders wrong.

### Gate 5 — reading the screenshots

The walkthrough passing is not the same as the UI being right. Actually looking at the
output found three more: fraud severity rendering backwards (`CRITICAL` was missing from the
tone map, so the most urgent cases were the calmest colour), stat labels unreadable where a
translucent wash sat over a dark ground, and raw 4-decimal ledger values (`366000.0000`)
leaking into the send-money form.

**Automated checks prove the page loaded. Only looking proves it is right.**

### The bug that justifies all of this

`do_validate` was throwing away the beneficiary signals that `account` returned — the payee
fingerprint, its age, whether it was inside its cooling-off window. `do_screen` then read
those from `txn.context` and found them empty.

**Every payee-dependent fraud rule silently never fired.** A ₹95,000 international transfer
to a brand-new payee inside its cooling-off window scored **0 — ALLOW**, indistinguishable
from a clean transfer.

It passed all 54 payments tests, because the test fixture omitted exactly the fields the
real service returns. It passed the demo script, because that script hand-fed the context.
It was found by looking at a screening result that should obviously have been high and was
zero.

The fix carries the signals forward from `account`. The fixture was corrected to return what
the real service returns. Four regression tests were added — including one asserting that a
**client cannot lie its way past a rule** by supplying its own payee signals, since these
must come from `account` and nowhere else. The fix was then proven by reintroducing the bug
and watching two tests fail.

> The lesson worth carrying: **a fake that is more convenient than the real thing will hide
> the bugs that matter.** The fixture was not wrong by accident — it was wrong by being
> tidy.

---

## 12. Defending this in a system design interview

### "Why microservices? Isn't this over-engineered for two use cases?"

For the volume, yes — a monolith would be simpler, and there is a superseded monolith design
in `docs/plan.md` that we deliberately moved away from.

The honest answer is that the *boundaries* are real even if the scale is not. Fraud
screening has completely different latency, availability and change-frequency requirements
from onboarding: it must answer in under 50ms, it must never be the reason money moves
incorrectly, and its rules change weekly. The ledger must be conservative and change almost
never. Putting those in one deployable means the fraud team's weekly rule change forces a
redeploy of the ledger.

What I would *not* claim: that we need ten services for throughput. We do not. We chose the
boundaries for **change isolation and blast radius**, not for scale.

### "Database-per-service — how do you handle a query that needs data from two services?"

You do not do it in the database. You either:
1. call the owning service (synchronous, on the money path), or
2. keep a small local projection built from events (like `fraud_screened_txn`).

The cost is real: no joins, eventual consistency on projections, and duplicated data. What
we buy is that a schema change inside `ledger` cannot break `onboarding`, because
`onboarding` physically cannot reach those tables.

The counter-question I would expect: *"then why did you keep a copy of transactions in
fraud?"* Because velocity rules need to count recent transactions in under 50ms, and a
synchronous call to `payments` on every screen would put `payments` on the critical path of
its own critical path. The copy is deliberately minimal (nine columns) and pruned.

### "Why sagas instead of two-phase commit?"

2PC needs a coordinator that all participants trust and a lock held across services for the
duration. That means one slow participant blocks everyone, and a coordinator crash leaves
locks held. Nobody runs 2PC across HTTP services for good reason.

The saga trade-off is honest: we give up isolation. There is a window where the hold exists
but the payment has not been captured, and an observer can see that intermediate state. We
accept that because **the intermediate state is meaningful to the user** — "your money is
held while we check this" is a sentence a customer understands.

### "What happens if compensation fails?"

This is the question that separates a real saga from a diagram. Our answer:
`COMPENSATION_PENDING` is a first-class transaction state, the failed step is recorded as
`COMPENSATION_FAILED` with its error, and a scheduled task retries it. Ops sees it in the
failures console.

We do not claim it can never get stuck. We claim it **cannot get stuck invisibly**.

### "Why fail closed on fraud outage? You're blocking good customers."

Yes, and that is the point. If we fail open, the cheapest attack on this bank is a denial of
service against the fraud service — knock it over, then push everything through. Failing
closed means an outage costs us analyst hours and customer patience; failing open means an
outage costs us money we cannot get back.

It is written down as ADR-005 precisely because it is the kind of decision that gets quietly
reversed at 2am during an incident.

### "Why is the fraud rule language JSON instead of Python?"

Because the requirement is "an admin can tune rules at runtime", and the naive
implementation of that is `eval(rule.condition)`, which is remote code execution with extra
steps.

The JSON DSL has four node types and nine whitelisted operators. Unknown operators and
unknown facts are rejected at save time, so a broken rule fails when an admin clicks Save,
not at 3am on the money path.

The cost: the language is limited. A rule that needs something the DSL cannot express
requires a code change. That is a trade I would make again — the set of things a fraud rule
needs to say is small and well understood.

### "Why Django Q2 rather than Celery + Redis, or Kafka?"

Constraint-driven, and I would say so plainly: this environment has no Redis and no Kafka
available. Django Q2 uses the existing database as its broker.

But it is not purely a compromise. Because the queue is in the same database as the business
tables, **enqueuing a job and committing a business change are the same transaction**. With
Celery + Redis you get the classic bug where the job runs before the transaction that
created its row has committed. We get that correctness for free.

What we give up: throughput (a database is a worse queue than Kafka at volume), and
cross-language consumers. At our scale, neither binds.

### "Why SQLite? That's not a production database."

It is not, and I would not ship it. The constraint was no database server on the machine.

What matters is that **the design does not depend on it.** Every service uses the Django ORM
against its own database with its own connection. Moving to Postgres is a settings change
per service, not a rewrite. The one place SQLite genuinely leaked into the design is
`MoneyField` — Django's `DecimalField` round-trips through a float on SQLite, so we store
integer minor units instead. That change is *more* correct on Postgres too, so it is not
technical debt.

I would flag the honest weakness: SQLite's write concurrency is a single writer per
database. Our optimistic-locking tests pass, but they are not proving what they would prove
under Postgres's MVCC.

### "How do you know a payment isn't processed twice?"

Three independent layers, and I would want to be asked about all three:
1. **API layer** — `payments_idempotency` unique on `(user_id, key, endpoint)`, with a
   `request_hash` so a reused key with a different body is a `409` rather than a wrong
   replay.
2. **Ledger layer** — `ledger_journal_entry.idempotency_key` and `ledger_hold.idempotency_key`
   are both unique. Even if the saga somehow retried a capture, the ledger refuses it.
3. **Event layer** — `pc_inbox_event.event_id` is the primary key, so redelivery is a failed
   insert.

The layers are independent on purpose. A bug in one does not defeat the other two.

### "Your audit log claims to be tamper-evident. Is it?"

Partially, and I would be careful here rather than overclaim. `row_hash = sha256(prev_hash +
row content)` means editing a historical row breaks every hash after it, and
`verify_chain` detects that.

**What it does not stop:** someone with database write access can recompute the entire chain
from the edited row forward, and it will verify cleanly. Real tamper-*proofing* needs the
head hash anchored somewhere we do not control — periodically published externally, or
signed with a key the database user does not hold.

So the accurate claim is: it detects casual and accidental tampering, and it makes
undetected deliberate tampering require rewriting the whole chain rather than one row. That
is a meaningful bar, but it is not the same as impossible.

### "What's the weakest part of this design?"

Three things, and I would volunteer them rather than wait:

1. **The saga orchestrator is a single point of coordination.** `payments` going down stops
   all money movement. Sagas in flight are recovered by a sweeper, but there is no failover.
2. **No real outbox relay throughput story.** Delivery is per-row over HTTP. At volume you
   would batch, and you would want a proper broker.
3. **`audit` has no tests** — the service whose entire value proposition is "you can trust
   this record" is the one service with no test proving it. That is the wrong place to have
   the gap and it is the first thing I would fix.

---

## 13. Edge cases — what breaks, and what we do

### Money and concurrency

| Case | What happens |
|---|---|
| Two payments race on the same balance | Optimistic lock on `ledger_balance.version`; loser retries against fresh state |
| Customer double-clicks Send | Same idempotency key (minted at form open) → original response replayed |
| Retry with the same key but a changed amount | `409 Conflict` — never a silent wrong replay |
| Retry while the first is still running | `409` — the answer does not exist yet |
| A hold is never resolved | `ledger.tasks.expire_holds` releases holds past `expires_at` |
| Rounding drift over many operations | `ROUND_HALF_EVEN`, not `ROUND_HALF_UP` |
| A float sneaks into an amount | Impossible on the wire — amounts are strings; storage is integer minor units |
| Sub-paisa FX precision | 4 decimal places, not 2 |

### The saga

| Case | What happens |
|---|---|
| A step fails midway | Completed steps unwind in reverse order |
| The *unwind* fails | `COMPENSATION_PENDING` + `retry_compensation` task; visible to ops |
| The service dies mid-saga | `sweep_stuck_sagas` finds transactions stuck in non-terminal states |
| An analyst approves a held case | Saga resumes at **CAPTURE**, not from the start — the hold is still valid |
| A held case is never reviewed | `expire_stale_reviews` expires it; hold released |
| Customer cancels a dispatched payment | Refused — `CANCELLABLE_STATUSES` excludes it. The correct action is a return |
| A scheduled transfer fires while the account is frozen | Fails at VALIDATE; `schedule.failed` → notification + ops |

### Fraud

| Case | What happens |
|---|---|
| The fraud service is unreachable | **REVIEW**, never ALLOW |
| A brand-new account with no history | R013 (first transaction, large) covers the cold-start case; z-score rules need `txn_count` before they contribute |
| An admin writes a broken rule | Rejected at save time, not at runtime |
| An admin writes a *valid but bad* rule | Dry-run against 30 days of traffic shows the blast radius first; new rules default to SHADOW |
| A client supplies its own payee signals | Ignored — payee facts come from `account` only. There is a test asserting a client cannot lie past a rule |
| Duplicate screening of the same transaction | `fraud_decision` unique on `txn_ref` |
| Velocity rules contaminated by test runs | Real behaviour, not a bug — the seeder waits out the velocity window |

### Events

| Case | What happens |
|---|---|
| The same event is delivered twice | `pc_inbox_event.event_id` is the PK; duplicate insert fails, returns `200`, producer stops |
| An event arrives out of order | `pc_projection_cursor.last_sequence` lets a consumer ignore a stale one |
| A subscriber is down | Exponential backoff via `next_attempt_at`; the outbox row persists |
| A subscriber is down permanently | Dead-lettered after the attempt limit → `outbox.dead` → ops failure case |
| An event is published outside a transaction | `PublishedOutsideTransaction` is raised — a loud failure, not a silent race |
| A signed request is replayed later | `X-Signature-Timestamp` outside the window is rejected |
| An event has no subscribers | Logged as a warning; delivered to `audit` regardless via `"*"` |
| A failure keeps re-firing | One `ops_failure_case`, not one per retry (unique on `failure_type, subject_ref`) |

### Auth

| Case | What happens |
|---|---|
| A stolen refresh token is used | The whole family is revoked; `security.refresh_reuse_detected` fires |
| Five parallel requests hit an expired token | Coalesced into one refresh — otherwise four look like theft |
| Repeated failed logins | `failed_logins` + `locked_until`; `unlock_expired_lockouts` clears it |
| Someone tries to register as an admin | Impossible — the public endpoint always creates a CUSTOMER |
| A signing key is rotated | JWKS serves both; old tokens verify until they expire |
| XSS reads `localStorage` | Finds no access token — it lives in a module variable |
| A staff URL is typed directly by a customer | Server-side role check returns 403; the SPA guard is convenience only |

### Data handling

| Case | What happens |
|---|---|
| A raw card number is submitted | Rejected with an explanation; only vault tokens are stored |
| A beneficiary account number is returned | Masked |
| KYC identity data ages out | `purged_at`; the indexed `national_id_hash` survives for duplicate detection |
| The same person applies twice | `kyc_case` unique on `application_id`; hash matching catches the repeat |
| A malformed IFSC or SWIFT | Rejected at the serializer, before it reaches any service |
| The same beneficiary added twice | Unique on `(user_id, fingerprint)` |
| A payee is deleted | It is not — DELETE blocks the beneficiary. Payment history must keep referring to something |

---

## 14. What is not done

Stated plainly, because a handbook that only lists strengths is marketing.

**`audit` has zero tests.** Its `tests/` directory holds nothing but `__init__.py`, and
`test_all.py` silently skips any service without test files — so the run still prints "all
suites passed". Nothing anywhere proves that chain verification actually detects a tampered
row. This is the highest-value gap in the repo, because it is precisely the service whose
value is the guarantee.

**No frontend test suite.** No Vitest, no React Testing Library. The only frontend
verification is the Playwright walkthrough, which loads pages but does not exercise
interactions.

**No interactive browser testing.** Submitting a transfer, approving a case, and saving a
rule are all verified at the API level and render correctly, but have never been clicked
through end to end in a browser. The wiring between a rendered form and a proven endpoint is
the untested seam.

**No responsive testing.** The walkthrough runs at 1440×960 only. Responsive CSS exists and
is unexercised.

**No accessibility pass.** No keyboard navigation testing, no screen reader testing. Colour
contrast was checked by eye after a bug, not systematically.

**There is no circuit breaker, despite the HLD saying there is.** `01-hld.md` describes a
breaker that trips after N failures and half-opens to drain the backlog. The code has none —
`do_screen` simply catches `ServiceUnavailable` and halts to `UNDER_REVIEW`. The *behaviour*
is correct (fail closed, hold retained), but every request keeps hammering a dead service
instead of failing fast. This is documentation ahead of implementation, and an interviewer
reading both would catch it.

**Rails are simulated.** So are the KYC provider and the sanctions lists. The interfaces are
real; what sits behind them is not.

**SQLite write concurrency.** The optimistic-locking tests pass, but a single-writer
database is not exercising what Postgres MVCC would exercise.

**No load testing.** The "under 50ms" fraud screening claim is from local timing on a
near-empty database, not from a benchmark under realistic data volume.

---

## Where to go next

| I want to… | Read |
|---|---|
| Understand why the boundaries are where they are | [`microservices/00-first-principles.md`](microservices/00-first-principles.md) |
| See the architecture in detail | [`microservices/01-hld.md`](microservices/01-hld.md) |
| See per-service internals | [`microservices/02-lld.md`](microservices/02-lld.md) |
| See every event and its payload | [`microservices/03-events.md`](microservices/03-events.md) |
| See every endpoint and its contract | [`microservices/04-api-contracts.md`](microservices/04-api-contracts.md) |
| Set up and run the frontend | [`../client/README.md`](../client/README.md) |
