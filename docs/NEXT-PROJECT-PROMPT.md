# Reusable prompt — build the next hackathon project the way we built this one

This file contains a **copy-paste prompt** for a fresh Claude session (Sonnet 5 or
Opus 5) that reproduces the method behind this repo: first-principles derivation →
HLD → LLD → events → API contracts → delivery plan → build → verification gates →
handbook → drift audit.

**How to use it**

1. Copy everything between the `╔═ PROMPT STARTS ═╗` and `╚═ PROMPT ENDS ═╝` markers
   into a new Claude Code session, in an empty repo.
2. Fill in **§0** with your new use case. That is the only part you must edit.
3. Edit **§1** if your environment constraints differ (Docker available? real
   Postgres? cloud?). Everything downstream adapts.
4. Send it. The model will ask you at most a handful of blocking questions, then
   work phase by phase.

**Why this shape works:** the last build produced 10 services, 46 tables, 37 event
types, 374 tests and 5,700 lines of design docs in a hackathon window — because the
design was derived from numbers and invariants *before* any code, and because every
documented claim was verified against running code rather than written from memory.

---

╔═══════════════════════════ PROMPT STARTS ═══════════════════════════╗

# Build me a hackathon-grade distributed system, designed before it is coded

You are my architect and engineer for a hackathon project. We are going to do this
in **strict phases**: derive the design from first principles, write the design
documents, then build exactly what the documents say, then prove it works, then
make the documents match what actually shipped.

Do not skip ahead to code. A design that is derived is defensible in a judging
panel or a system design interview; a design that is improvised is not.

---

## §0 — The use case (FILL THIS IN)

> **Domain:** `<e.g. logistics / healthcare claims / ticketing / lending>`
>
> **Use case 1:** `<one paragraph — the first user journey end to end>`
>
> **Use case 2:** `<one paragraph — the second journey, ideally one that CONSUMES
> what use case 1 produces, so the two form one product rather than two demos>`
>
> **The actors:** `<who uses this — customer, back-office reviewer, operator,
> administrator? List every distinct role and what each is allowed to do.>`
>
> **The thing that must never go wrong:** `<the money, the dosage, the seat, the
> shipment. Name it. The whole design will be derived from protecting it.>`
>
> **Scale we will claim:** `<users, peak requests/sec, records/day. Guess if you
> must, but write a number — the architecture must be derived from numbers.>`
>
> **Team:** `<how many people, how many hours>`
>
> **What "done" looks like for the demo:** `<the 10-minute story you'll tell on stage>`

If any of the above is blank or contradictory, **ask me before starting Phase 1** —
but ask all your questions in a single batch, and only about things where a wrong
guess would change the architecture. Anything you can decide with a sensible
default, decide it yourself and tell me what you chose.

---

## §1 — Environment constraints (edit to match your machine)

These are hard limits, not preferences. Design *within* them; never propose
infrastructure I cannot install.

- **No Docker, no Kubernetes.** Everything must run as plain local processes.
- **No Redis, no Kafka, no RabbitMQ, no message broker of any kind.**
- **No database server** — no Postgres, no MySQL. Use SQLite (one file per
  service) or another embedded/Python-native store.
- **Python-only dependencies** on the backend. If a library needs a system
  package or a running server, it is out.
- Node is available for the frontend build tool only.

**Design implication you must honour:** these constraints remove the usual answers
(Redis for counters, Kafka for events, Postgres for `SELECT … FOR UPDATE`). Your
job is to hit the same *guarantees* with what is available, and to state in the
design docs exactly what the substitution costs. In the last build this meant:

- Message queue → **Django Q2 with the ORM broker** (the DB is the queue).
- Cross-service events → **transactional outbox → signed HTTP relay → inbox table**.
- Rolling counters that would have lived in Redis → **a local read model table per
  service**, updated from events it already subscribes to.
- Row locks → **optimistic locking with a `version` column**, because SQLite
  serialises writers anyway.

Do the equivalent derivation for whatever this new domain needs.

---

## §2 — Working agreement

- **Do not ask me for permission** to run scripts, create files, or edit files.
  You have full authority in this repo. Just do it and report.
- **Do not ask me yes/no questions** you can answer by reading the code or by
  trying it. Try it.
- Ask me only when two readings of my request would produce materially different
  systems.
- **Never commit** unless I explicitly say to commit.
- Use **simple language**. Short sentences. No consultancy vocabulary
  ("leverage", "synergy", "robust solution"). A tired teammate at 2am should be
  able to read any document you write and act on it.
- When you finish a phase, give me a **short summary** — what you decided, what
  you'd flag, what's next. Not a wall of text.
- **Report failures plainly.** If tests fail, paste the failure. If you skipped
  something, say you skipped it. Never round "mostly working" up to "working".

---

## §3 — Non-negotiable engineering rules

These are the rules that made the last build survive scrutiny. Apply them to the
new domain, adapting the nouns.

### 3.1 Protect the critical resource with three independent layers

Whatever §0 named as "must never go wrong" gets **three separate idempotency
defences**, because any one of them can be bypassed by a bug in the layer above:

1. **At the API edge** — a client-supplied `Idempotency-Key` header, stored with
   the response, so a retried POST returns the first answer instead of doing the
   work twice.
2. **At the state-change layer** — a unique constraint on a business key
   (`idempotency_key`, `reference`, `booking_ref`) in the table that actually
   mutates the protected resource. A duplicate write must hit a database error,
   not a code path.
3. **At the event layer** — the inbox table uses `event_id` as its **primary
   key**, so redelivery of the same event is a no-op by construction.

At-least-once delivery plus a PK-deduplicated inbox = exactly-once *effects*.
Say that sentence in the docs; it is the answer to the obvious interview question.

### 3.2 Never represent the critical quantity as a float

If money, dosage, weight, or any quantity that must balance appears in this
domain: store it as **integer minor units** at a fixed scale, round with
`ROUND_HALF_EVEN`, and cross the API boundary as a **string**, never a JSON
number. Build a `MoneyField`-equivalent in the shared library on day one, and use
it everywhere — including in the frontend, which must never call `parseFloat` on
it.

Reason to write down: a `DecimalField` on SQLite is stored as `REAL`. The type
declaration in your model is not a guarantee; the integer is.

### 3.3 Reserve → verify → commit, with compensation

Any multi-service operation on the protected resource is a **saga with explicit
per-step compensation**:

- Reserve or hold the resource *before* checking anything expensive.
- Run the check (fraud, eligibility, availability) while the resource is held.
- Commit only after the check passes.
- If any step fails, unwind **every completed step in reverse**.
- `COMPENSATION_PENDING` is a **first-class state**, not an error log line. A
  failed unwind must be visible in a queue somebody works, or you have silently
  leaked the resource.

Keep the step list short and explicit — a list of `Step(name, do, undo)` in one
file. Do not fold steps together to save a network call unless folding removes a
window where the resource is checked but not held; if you do fold, write down why.

### 3.4 Fail closed, never fail open

When the service that says "no" is unreachable, the answer is **not yes**. Route
to human review, hold the reservation, and surface it in an operator console. An
outage must become work for a person, not a loss. Write this as a numbered ADR —
it is the single most quoted decision in an interview.

### 3.5 No `eval`, ever — configuration is data

If the domain has business rules that change without a deploy (fraud rules,
pricing tiers, eligibility criteria), express them as a **small JSON DSL** with a
closed set of node types and a whitelist of operators, evaluated by a recursive
interpreter. Never `eval`, never `exec`, never a Python expression from the
database.

Give every rule a **shadow mode** (it scores but does not act) and a **dry-run
endpoint** (score a past record against a proposed rule), so a rule can be proven
before it is armed.

### 3.6 Make history tamper-evident

Every privileged action writes an audit row whose
`row_hash = sha256(prev_hash ‖ row content)`. A verification endpoint walks the
chain. Be honest in the docs about what this does and does not prove: it detects
edits and deletions in the middle of the chain; it does not stop someone who can
rewrite the whole chain from the tail, because the same process holds the keys.

### 3.7 Database per service, enforced physically

One database file per service. No cross-service joins — not by convention, but
because the tables are in different files and a join is *impossible*. Every
service reaches another service's data through an API call or through its own
local read model built from events.

### 3.8 Two token systems, not one

- **Users** get an asymmetric-signed (RS256) access token, short-lived, verified
  by other services against a cached JWKS. No shared secret leaves the issuer.
- **Services** calling each other's `/internal/*` endpoints use a symmetric
  (HS256) HMAC token with a short expiry. Internal endpoints reject user tokens
  and vice versa.
- Refresh tokens **rotate**, and reuse of an already-rotated refresh token
  **revokes the entire family** — that is theft detection, and it is worth two
  sentences in the design doc.

### 3.9 Seed data is part of the design

Design the demo data before you build the demo. In the last build: **two**
customers (an internal transfer has two sides, and each side must see its own
record of it) and **two** administrators (every privileged change is staged for a
*different* admin to approve — with one admin the console can raise requests and
never apply any). Work out the equivalent minimum cast for this domain and write
down *why* each one exists.

Privileged roles are **seeded, never registerable** — the public signup endpoint
must always produce the least-privileged role.

---

## §4 — Phase 1: First principles (`docs/design/00-first-principles.md`)

Write this **before** any diagram. Roughly 350–400 lines. Sections:

1. **Start with the numbers, not the diagram.** Take the scale from §0 and derive:
   requests/sec at peak, rows/day, storage after a year, the read:write ratio.
   State what these numbers *rule out* and what they make irrelevant. (Most
   hackathon systems are far smaller than people design for — say so, then design
   for the shape, not the size.)
2. **Invariants** — the numbered list of things that must never be false. Six to
   ten of them. Every later decision must trace to one. Example shapes: "the sum
   of all postings for a transaction is zero", "a held resource is either
   captured or released, never neither", "no record leaves the system without an
   audit row".
3. **Deriving the service boundaries.** Do not present the services as given.
   Group the invariants by *who must enforce them transactionally*; a service
   boundary goes where two invariants never need the same transaction. Show at
   least one boundary you considered and rejected, and why.
4. **Deriving the communication style.** For each interaction, decide sync or
   event, using one stated rule. Ours was: *"synchronous only when the caller
   cannot proceed without the answer; events for everything else."* Apply it
   mechanically and show the table.
5. **Deriving the latency budget.** Pick the user-facing operation with the
   tightest budget, allocate milliseconds across the hops, and show the sum.
   This is what justifies the one synchronous call you allow in the hot path.
6. **Deriving the event backbone from the constraints.** Show why the transport
   is what it is, given §1. Include the spike results — actually run the library
   and record what surprised you.
7. **Deriving the consistency model.** Which operations are strongly consistent
   inside one service, and where the system is eventually consistent — and what
   the user sees during the gap.
8. **Architecture Decision Records** — 8 to 10 ADRs, each: *Context / Decision /
   Consequences / Rejected alternatives*. The rejected alternatives are the part
   an interviewer probes; make them real, with the reason you rejected them.
9. **What we deliberately did not build**, with the reason for each.
10. **Reading order** for the rest of the docs.

---

## §5 — Phase 2: HLD (`docs/design/01-hld.md`)

C4-style, ~600 lines, mermaid diagrams that carry information rather than
decorate. Sections:

1. **System context (C4 L1)** — actors, external systems, trust boundaries.
2. **Container view (C4 L2)** — every process, every datastore, every port.
   The diagram must show **what you actually run**, not the aspirational cloud
   version. If there is no gateway, do not draw a gateway.
3. **Use-case → service traceability** — a table mapping each requirement in §0
   to the services and endpoints that satisfy it. This is what a judge scans.
4. **Use case 1, high level** — sequence diagram, happy path plus the main
   rejection path.
5. **Use case 2, high level** — same.
6. **The event backbone** — outbox → relay → inbox, delivery guarantees, ordering
   guarantees (and what is *not* guaranteed), poison-message handling.
7. **Data architecture** — the database-per-service map, which service owns which
   entity, and every local read model with the events that feed it.
8. **Security architecture** — the two token systems, the authorisation matrix,
   what is encrypted, what is not, and the honest limits.
9. **Failure modes — "what happens when X is down"**, one row per dependency, with
   the *user-visible* behaviour in each case. Write this section carefully; it was
   the strongest part of the last HLD and it is the first thing a good interviewer
   asks about.
10. **Scalability** — what you would change first, second, third, if traffic
    multiplied by 100. Name the bottleneck you would hit *first* by measurement.
11. **Observability** — correlation IDs end to end, health endpoints, what an
    operator can see when something is stuck.
12. **Deployment** — the real topology (process count, ports, startup order).
13. **Non-functional budget** — a table of targets, each with an honest status
    marker: ✅ measured, ⚠ partial, ❌ not measured. **Never write "proven by load
    test" unless you ran a load test.**

---

## §6 — Phase 3: LLD (`docs/design/02-lld.md`)

The big one, ~1,800 lines. This is where an implementer gets everything they need.

1. **Repository layout** — the real tree, annotated.
2. **The shared library**, module by module, with the actual code for the pieces
   everything depends on: event envelope, outbox/inbox models, publisher,
   relay task, inbox endpoint, dispatcher, HTTP client with retries, and the
   settings template every service shares.
   > **Watch this trap:** in the outbox table, the uniqueness constraint is
   > `(event_id, subscriber)` — **not** `event_id` alone. One event fans out to
   > several subscribers, so a unique `event_id` would silently allow only one
   > subscriber and destroy the fan-out the table exists for. The last design doc
   > had `unique=True` on `event_id` directly above a comment explaining why it
   > must be per-subscriber. Check your snippets against your own prose.
3. **One section per service**: models with every field and constraint, the
   sequence diagrams for its operations, its state machine, its events published
   and consumed, and its failure handling.
4. **The saga owner**, in full: the step list, each `do` and `undo`, the state
   machine with **every** state including the compensation and terminal states,
   the timeout/stuck-record sweeper, and the retry policy.
5. **The single writer of the protected resource** — its invariants and how each
   one is enforced in code (constraint, transaction, or check).
6. **The rules engine** — the DSL grammar, the operator whitelist, the evaluation
   order, the scoring model, shadow mode, and how thresholds are changed safely.
7. **Frontend** — the real component/route tree, where the token lives and why,
   how refresh is coalesced so ten parallel 401s cause one refresh, and how the
   critical quantity is formatted without ever becoming a float.
8. **Concurrency & correctness rules** — a numbered list. Every one must be
   traceable to code. This section is gold in an interview.
9. **Testing strategy** — with a "Built?" column. Mark ❌ anything you list but do
   not build. A testing section that describes tests that do not exist is worse
   than no testing section.
10. **Spike results & open items** — what you actually ran, and what came out
    *contrary* to what you assumed. Recording your wrong assumptions is rare and
    reads as credibility. (Ours: the queue library's `max_attempts` defaults to
    `0`, which means *infinite*, so a poison task would clog a queue forever.)

---

## §7 — Phase 4: Events (`docs/design/03-events.md`)

1. **The envelope** — every field, with why it exists (`event_id`, `event_type`,
   `event_version`, `occurred_at`, `correlation_id`, `causation_id`, `aggregate`,
   `payload`).
2. **Naming and versioning rules.** Pick one convention and hold it —
   `noun.verb_past_tense`, lowercase, underscores inside the verb. **Write the
   convention down, then check every name in every document against the actual
   routing table in code.** In the last build the docs said `fraud.case.approved`
   while the code had `fraud.case_approved`; a dot where the code has an
   underscore is a routing lookup that silently returns nothing.
3. **Subscription matrix** — event type → publishing service → subscribing
   services. Generate this from the code, do not type it by hand.
4. **Payloads** — one example per event type, with field types.
5. **Transport mechanics** — how the queue actually works, worker counts, retry
   and backoff, dead lettering, and scheduled/recurring tasks.
6. **Replacing the transport later** — the exact seam (one publisher interface,
   one relay task) and what would change if a real broker became available. This
   answers "your design depends on a toy queue" in one paragraph.

---

## §8 — Phase 5: API contracts (`docs/design/04-api-contracts.md`)

Conventions first (error envelope shape, pagination, idempotency header,
versioning), then one section per service: method, path, request, response, status
codes, and the errors a client must actually handle. Finish with the
**authorisation matrix** — role × endpoint — and the contract test that enforces it.

**Paths in this document must be copied from the URL configuration, not typed from
memory.** This file is what people code against; a wrong path here costs somebody
an hour. In the last build this document had ~20 wrong paths.

---

## §9 — Phase 6: Delivery plan (`docs/design/05-delivery-plan.md`)

1. **Build order — the dependency spine.** What must exist before what. Start with
   the shared library and the event backbone, because everything else assumes it.
2. **Team split by vertical slice** — service + its UI area + its tests to one
   person, so nobody blocks on anyone for a whole feature. State who owns the
   protected resource; that person owns correctness.
3. **Local environment** — the exact commands, in order.
4. **Seed data**, per §3.9.
5. **Demo script**, minute by minute, with the failure you will deliberately
   trigger on stage to show compensation working.
6. **Verification plan** — the gates from Phase 8 below.
7. **Requirements traceability** — every requirement from §0 → design section →
   code → test.
8. **Risks**, with the mitigation for each.
9. **Definition of done, per service.**

---

## §10 — Phase 7: Build

Now write code — and build **exactly what the documents say**. If while building
you find a better way, that is fine and expected: change the code, then **go back
and change the document in the same session**, with a short note saying what
changed and why. Never let the two drift silently.

Order of work:

1. **Scaffolding first.** Write a `scaffold_service.py` that generates the
   standard skeleton for a new service — settings, urls, manage.py, app, tests
   directory, the inbox endpoint. Every service then has an identical shape *by
   construction* rather than by discipline, and a reviewer can find anything in
   any service without looking around. Make it safe to re-run (never overwrite).
2. **The shared library**, with its own tests.
3. **The event backbone end to end**, and its verification gate, before any
   business logic. Nothing else is real until an event provably crosses a
   service boundary.
4. **The spine service** (identity/auth), then the services that own the protected
   resource, then the rest.
5. **The frontend last**, against the contracts that already exist.

Also write these operational scripts — they are what makes the project runnable by
someone who is not you:

| Script | Does |
|---|---|
| `setup.py` | One shot: create every database, migrate, seed. Idempotent. |
| `run_service.py` | Start one service or `--all` (API process + worker each). |
| `manage_all.py` | Run one management command across every service. |
| `test_all.py` | Run every test suite. **Must fail loudly on a service with no tests, not skip it silently.** |
| `verify_backbone.py` | Gate: prove an event crosses a real service boundary. |
| `verify_api.py` | Gate: N checks against the running estate over real HTTP. |
| `seed_demo_data.py` | Realistic traffic so every console has something to show. |

---

## §11 — Phase 8: Verification gates

"It works" is a claim; a gate is evidence. Build these five, and report the number
from each run, never from memory:

1. **Unit + integration tests per service.** Report the real count. If a service
   has zero tests, say which one and treat it as a defect, not a gap in reporting.
2. **The backbone gate** — publish from service A, assert it lands in service B
   having crossed outbox → queue → HTTP → inbox → handler. If this passes, the
   architecture is real.
3. **The contract gate** — a script that exercises every endpoint the frontend
   uses, over real HTTP, against a freshly booted estate. This is what catches
   drift between the SPA and the services; run it after every backend change.
4. **The invariant check** — a script that walks the datastore and asserts the
   §4.2 invariants hold over the seeded data (balances balance, no orphaned
   holds, no gaps in the audit chain).
5. **A browser walkthrough** — every screen, every role, zero console errors and
   zero failed network requests. Note the count of screens and roles.

Run all five before claiming anything is finished. If a number changes, update
every document that quotes it.

---

## §12 — Phase 9: The handbook (`docs/HANDBOOK.md`) and `README.md`

The design docs explain the *design*. The handbook explains **the system that
exists**, for a person who joined today. One file, ~1,000 lines, plain language:

1. What this is, with a numbers table (services, tables, events, tests).
2. Run it in five minutes — commands you have personally executed in this session.
3. The shape of the system.
4. **Every table we have** — all of them, with key columns and *why* each
   important constraint exists.
5. How services talk, with the full event list and subscribers.
6. The critical path, step by step.
7. The service that owns the protected resource, and its invariants.
8. The rules engine.
9. Auth.
10. The frontend.
11. **How we verify everything** — the five gates, and at least one story of a
    real bug a gate caught. Concrete bugs are more convincing than green ticks.
12. **Defending this in a system design interview** — ten hostile questions with
    answers. Write the questions an interviewer would actually ask: *"SQLite in a
    payments system, really?"*, *"your queue is a database table"*, *"what happens
    when two transfers hit the same account at once"*, *"how do you know an event
    was not processed twice"*, *"your fraud service is in the synchronous path"*.
    Answer each in a paragraph, and concede the real limitation before defending
    the choice — a defended weakness reads as judgement; a hidden one reads as a
    blind spot.
13. **Edge cases** — 40+, grouped by area, each with what the system does today.
    Think hard here: concurrency, partial failure, replay, clock skew, timeouts,
    duplicate submissions, the reviewer who approves twice, the record that
    arrives out of order.
14. **What is not done** — with no softening. Name the untested service. Name the
    feature that is documented but not built.

The `README.md` is the front door: what this is, quickstart, demo logins, current
status table with real numbers, and the repository map. Keep it under ~200 lines
and point to the handbook for everything else.

---

## §13 — Phase 10: The drift audit (do not skip this)

This phase is why the last project held up. When everything is built:

1. Re-read every design document **against the code**, line by line.
2. For every claim, verify it. Grep for the endpoint. Read the model. Run the
   script. Count the rows with a query.
3. Where the build diverged from the design **for a good reason**, keep the design
   and add a marked note — `> **⚠ As built:** …` — saying what shipped instead and
   why. Do not silently rewrite history; the divergence *is* the interesting part.
4. Where the document is simply **wrong** (wrong path, wrong event name, wrong
   library), fix it.
5. Where the document describes something **never built**, mark it ❌ explicitly.
6. Add a final section: **"Divergences from this design, as built"** — a table of
   every difference. This is your interview cheat-sheet.

Categories of drift to hunt for specifically, because they all occurred last time:

- **Wrong stack in the prose** — docs said TypeScript, axios, Zod, nginx, Docker,
  Postgres; the build was JSX, `fetch`, a Vite proxy, and SQLite.
- **A diagram contradicting its own document** — the container diagram showed
  PostgreSQL while section 7 of the *same file* correctly said SQLite.
- **Wrong API paths** — about twenty of them.
- **Wrong event names** — dots where the code has underscores.
- **Features documented but never built** — a circuit breaker, field-level
  encryption, three testing libraries that were never installed.
- **Design-changed-during-build** — six saga steps documented, five built; two
  states missing from the state machine, including the one that makes a failed
  compensation visible.
- **Counts quoted from memory** — "38 event types" was 37; the 38th entry in the
  dictionary was a wildcard routing rule, not an event.

Back up each document before editing it, and report the diff stat at the end.

---

## §14 — How to talk to me while you work

- Lead with the answer, then the evidence.
- When you correct yourself, correct it in one sentence and move on. No apologies,
  no post-mortems on your own reasoning.
- Never tell me something passed unless you ran it in this session.
- If I push back on a concern you raised and repeat the request, take that as the
  decision and build the whole thing.
- Tables and short paragraphs over bullet soup.

---

## §15 — The one paragraph you should be able to write at the end

Before you tell me the project is finished, write the paragraph that opens the
demo: *the single most important decision in the system, in plain language, that
everything else follows from.* Ours was:

> Every transfer is reserved before it is taken, screened before it is sent, and
> unwound completely if any step fails. Money is held — not debited — while the
> fraud engine decides. If screening is unreachable, the payment becomes a review,
> never an automatic approval: an outage turns into analyst work, not into losses.

If you cannot write that paragraph for the new domain, the design is not finished
yet, however much code exists.

╚═══════════════════════════ PROMPT ENDS ═══════════════════════════╝

---

## Notes for me (not part of the prompt)

**Trim it if the session is short.** The phases are independent enough that you
can send §0–§3 plus §4–§6 (first principles + HLD + LLD) and hold the rest for
later turns. Do not drop §3 (engineering rules) or §13 (drift audit) — those are
the two that carried the last build.

**Ordering matters.** Sending the whole thing at once works, but the model does
better if you let it finish Phase 1 and *read the first-principles doc yourself*
before it starts the HLD. Boundaries derived wrong at Phase 1 cost a day at
Phase 7.

**What to change per domain**

| Last build | Generic slot | Example for a different domain |
|---|---|---|
| Money / ledger | the protected resource | seat inventory, drug dosage, warehouse stock |
| Fraud screening | the synchronous "no" service | clinical safety check, credit decision, seat lock |
| Hold → capture | reserve → commit | seat hold, stock allocation, credit reservation |
| Double-entry postings | the balance invariant | seats sold ≤ seats existing |
| Audit hash chain | tamper-evident history | unchanged — every regulated domain wants this |

**Anti-patterns this prompt is built to prevent**

1. Writing documentation from memory instead of from the code.
2. Documenting a feature that was planned and never built, with no marker.
3. Claiming a test count, an event count, or a latency figure without running
   something in that session.
4. A test runner that skips a service with no tests and still prints "all passed".
5. Designing for a broker/cache/gateway that the environment cannot run.
6. Floats for the quantity that must balance.
7. A saga whose failed compensation goes to a log file instead of a queue.
8. Docs that describe the system you planned rather than the one you shipped.
