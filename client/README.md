# IND Bank — web client

A Vite + React SPA over the ten backend services. Four role-based consoles and a
public homepage, all sharing one design system.

---

## Requirements

| | |
|---|---|
| Node | 20.19+ or 22.12+ (Vite 8) |
| npm | ships with Node |
| Backend | the estate running on ports 8001–8010 — see [`../server/README.md`](../server/README.md) |

Nothing else. No environment file, no API keys, no Docker.

---

## Quickstart

```bash
# 1. backend first — from the repository root
cd server
uv run python scripts/setup.py
uv run python scripts/run_service.py --all

# 2. demo sign-ins and some data worth looking at
(cd services/identity && uv run python manage.py seed_demo_users)
uv run python scripts/seed_demo_data.py

# 3. the client
cd ../client
npm install
npm run dev
```

Open <http://localhost:5173>.

### Demo sign-ins

All four use the password `demo-password-2026`.

| Email | Role | Lands on |
|---|---|---|
| `asha@indbank.test` | Customer | `/accounts` |
| `analyst@indbank.test` | Fraud analyst | `/fraud/queue` |
| `ops@indbank.test` | Operations | `/ops/queues` |
| `admin@indbank.test` | Administrator | `/admin/rules` |

---

## How it reaches ten services

The browser only ever talks to `http://localhost:5173`. The Vite dev server fans
`/api/*` out by path prefix (`vite.config.js`):

```
/api/auth          -> :8001   identity
/api/onboarding    -> :8002   onboarding
/api/accounts      -> :8004   account
/api/transactions  -> :8005   payments
/api/fraud         -> :8007   fraud
/api/notifications -> :8008   notification
/api/audit         -> :8009   audit
/api/ops           -> :8010   ops
```

The prefixes cannot collide: each service owns its own segment under `/api`.
Because everything is same-origin, CORS never arises in development. A service
that is not running returns a clean `SERVICE_UNAVAILABLE` envelope rather than a
proxy stack trace.

For a deployment without the proxy, each service also answers CORS directly —
set `CORS_ALLOWED_ORIGINS` on the backend.

`scripts/verify_spa_api.py` in the server tree uses this same routing table, so
a drift between the two is caught by a test rather than by a blank screen.

---

## Layout

```
src/
  styles/
    tokens.css        design tokens — colour, type, space
    base.css          reset, ledger-paper ground, type scale
    components.css    the boxy component kit
    shell.css         masthead, nav, page chrome
    home.css          the public homepage
  lib/
    api.js            fetch wrapper: tokens, refresh, error envelope
    auth.jsx          AuthProvider
    authContext.js    the context and useAuth
    roles.js          role constants
    money.js          money as strings, Indian digit grouping
    format.js         dates, status tone, rail labels
  components/
    kit.jsx           Panel, Money, Stamp, Field, Button, ScoreMeter…
    AppShell.jsx      chrome shared by all four consoles
    DocumentUpload.jsx
  routes/
    Home.jsx          public homepage
    Login/Register/Onboarding/Inbox
    customer/         accounts, send, add money, activity, payees, schedules
    analyst/          case queue, case detail, rule performance
    ops/              service health, failures, reports
    admin/            fraud rules, thresholds, limits, audit trail
```

---

## The design

**IND Bank — "ledger terminal".** Warm accounting paper, hairline rules forming a
visible structural grid, hard 2px frames, and a saffron accent.

**Zero border radius, everywhere.** There is deliberately no `--radius` token, so
nobody can reach for one. Corners are *marked* with crop-mark ticks rather than
softened.

Type is three families doing three jobs:

- **Instrument Serif** — the wordmark and page titles. Institutional weight.
- **Archivo** — all UI text. A sturdy grotesque that holds up at 11px.
- **IBM Plex Mono** — every number, always tabular, so columns align on the
  decimal.

Each role carries one accent colour, on the nav rule and under the wordmark —
saffron for customers, amber for fraud, blue for ops, violet for admin. Same
institution, unmistakably different desks.

Three colours are load-bearing and never reused for decoration: **allow** green,
**review** amber, **block** red. `BLOCK` additionally carries a diagonal hatch,
so the most consequential state in the product is still unmistakable to someone
who cannot separate red from green.

---

## Decisions worth knowing

**Money is a string from the wire to the screen.** It is never parsed into a
`Number` for arithmetic — `0.1 + 0.2 !== 0.3` and a bank cannot ship that.
`money.js` does Indian digit grouping (`12,34,567.89`) on the string itself,
because `Intl` would require the conversion we are refusing to make.

**The access token lives in a module variable, never in `localStorage`.** Anything
in `localStorage` is readable by any script that reaches the page. The refresh
token sits in `sessionStorage` so a reload does not sign you out.

**One refresh, shared.** A dashboard firing six parallel queries into the same 401
sends *one* refresh; the other five await the same promise. Without that, the
backend — which rotates refresh tokens and treats reuse as theft — would see five
replays and revoke the whole family.

**The idempotency key is minted when a form opens, not when Send is pressed.** A
second click, a flaky connection, a retry all carry the same key, so the server
replays its first answer instead of moving money twice. It resets only after a
completed payment.

**KYC documents are hashed in the browser.** Only the digest is sent. The
customer's passport scan never leaves their device.

**Polling, not WebSockets.** There is no Redis, so no Channels layer (ADR-007).
Queries poll while something is in flight and stop when it settles.

**`BLOCKED` and `UNDER_REVIEW` are outcomes, not errors.** They are what the fraud
engine is *for*. The transfer screen renders them as first-class results that say
plainly where the money is.

---

## Commands

```bash
npm run dev        # dev server on :5173 with the proxy
npm run build      # production bundle into dist/
npm run preview    # serve dist/ on :4173, proxy included
npm run lint       # eslint
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `SERVICE_UNAVAILABLE` on one screen | that service is not running — `run_service.py --all` |
| Sign-in fails for a demo user | run `manage.py seed_demo_users` in `services/identity` |
| Every transfer scores 100 | velocity rules from repeated seeding; wait five minutes |
| Application sits at `KYC_PENDING` | the whole chain must be up — kyc, onboarding and account |
| Onboarding lands in `MANUAL_REVIEW` | no documents attached, which is the correct behaviour |
| Empty analyst queue | run `scripts/seed_demo_data.py` |
