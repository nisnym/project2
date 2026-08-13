# backend — FastAPI microservices

Six independently runnable FastAPI apps. The SPA only ever talks to the
gateway; the gateway composes the five services behind it.

```
                    browser (React SPA :5173)
                              │
                              ▼
                    ┌──────────────────┐
                    │  gateway  :8080  │   composes, never decides
                    └────────┬─────────┘
        ┌──────────┬─────────┼──────────┬──────────────┐
        ▼          ▼         ▼          ▼              ▼
   customer   transaction  budget    insight         chat
    :9001        :9002     :9003      :9004         :9005
   who they    the ledger  limits +  advice, each  the advisor
   are        + all maths  pace      with a reason (GitHub Models)
        │          │         │          │              │
        └── customers.json   │          │              │
            accounts.json    │          │              │
                    transactions.json   │              │
                             budgets.json              │
                                        └── calls 9001/9002/9003
                                                       └── calls 9004/9003
```

Only two services own data. `budget`, `insight` and `chat` hold none at all —
they cannot answer a single question without calling their dependencies, which
is what makes "all the layers are live and connected" true rather than staged.

## Run it

```bash
./start.sh          # venv, deps, all six services, health-checked
./stop.sh           # stops everything it started
```

First run takes ~30s to build the virtualenv (uses `uv` if present, `venv`
otherwise). After that it's about two seconds.

| Service | Port | Docs | Owns |
|---|---|---|---|
| gateway | 8080 | http://localhost:8080/docs | nothing |
| customer-service | 9001 | http://localhost:9001/docs | `customers.json`, `accounts.json` |
| transaction-service | 9002 | http://localhost:9002/docs | `transactions.json` |
| budget-service | 9003 | http://localhost:9003/docs | `budgets.json` (limits only) |
| insight-service | 9004 | http://localhost:9004/docs | nothing |
| chat-service | 9005 | http://localhost:9005/docs | nothing |

Every service exposes `/health`, `/docs` and `/meta/stats`. Logs land in
`.run/<service>.log`.

```bash
curl localhost:8080/health/system    # one call, status of all six
```

## The endpoints that matter

```bash
# customer journey
curl localhost:8080/customers
curl localhost:8080/customers/cust-001/budgets
curl localhost:8080/customers/cust-001/insights
curl localhost:8080/customers/cust-001/health-score
curl localhost:8080/customers/cust-001/snapshot      # everything, one object

# bank journey — same ledger, read across the book
curl localhost:8080/bank/portfolio
curl localhost:8080/bank/alerts
curl localhost:8080/bank/customers/cust-002/review

# change something and watch it move
curl -X POST localhost:8080/customers/cust-001/transactions \
  -H 'Content-Type: application/json' \
  -d '{"amount": -2400, "category": "dining", "merchant": "Toit Brewpub"}'

# the advisor
curl -X POST localhost:8080/chat -H 'Content-Type: application/json' \
  -d '{"customerId": "cust-001", "message": "am I over budget anywhere?"}'
```

## The three calculations worth explaining

**Month-end projection** (`transaction_service/analytics.py:expected_remaining`)
A straight run-rate is wrong for real spending: someone who paid rent on the
2nd has not committed to paying it fifteen more times. So a category is split
in two — merchants that bill a flat amount monthly, and everything else. A
regular charge that has *already* billed this month contributes nothing;
everything else is averaged from what actually landed after today's
day-of-month in previous months. The method used (`history` or `run-rate`) is
returned with the number, and the sentence shown to the customer says which.

**Recurring detection** (`analytics.py:detect_recurring`)
Three tests, all required: seen in ≥2 months, exactly one charge per month,
and amounts within 2% (identical, if only seen twice). Strict on purpose —
two grocery runs that happened to cost the same are not a subscription, and
calling them one puts "consider cancelling DMart" in front of a customer.

**Health score** (`insight_service/rules.py:health_score`)
Budget discipline 35%, savings rate 30%, cash buffer 20%, spending stability
15%. Each component carries the sentence that justifies its number, and the
UI never shows the score without them.

## The chatbot

The advisor is grounded on `GET :9004/insights/{id}/snapshot` — the same
object the UI renders. The model is never handed the database, only that
snapshot, so any figure it quotes is one a judge can open in a browser tab.

```bash
CHAT_PROVIDER=github_sdk    # OpenAI-compatible SDK → GitHub Models (recommended)
CHAT_PROVIDER=github_cli    # shells out to `gh models run`
CHAT_PROVIDER=fallback      # deterministic rule engine, no network (default)
```

**GitHub Models via the SDK**

```bash
# a GitHub token with the models scope
echo 'GITHUB_TOKEN=ghp_xxx'        >> .env
echo 'CHAT_PROVIDER=github_sdk'    >> .env
echo 'GITHUB_MODEL=openai/gpt-4o-mini' >> .env
./start.sh
curl localhost:8080/chat/providers   # confirms what will actually be used
```

**GitHub Models via the CLI** — no token plumbing, just a logged-in `gh`:

```bash
gh extension install github/gh-models
echo 'CHAT_PROVIDER=github_cli' >> .env
```

What is already built: transport for both providers, the grounding snapshot,
the system prompt, JSON parsing that survives a model replying in prose, the
provider health report, and `GET /chat/prompt-preview?customerId=…&message=…`
which returns the exact prompt that would be sent. What is deliberately left
to tune: the wording of `prompt.py:SYSTEM_PROMPT` and the intent list — that
is the part worth iterating on with a real model in front of you.

`fallback.py` is not a stub. It answers from the same snapshot using the same
reasons, so the product works truthfully with no token, no network and no
model — and if a live provider fails mid-answer, the request falls through to
it rather than erroring.

## What is hardcoded (and where)

Everything about a *customer* lives in `data/*.json` and nothing else. These
are the only judgements baked into code:

| Thing | Value | Where |
|---|---|---|
| "Today" | `2026-08-12`, pinned | `shared/config.py:demo_today` — the seed ledger runs Jun–Aug 2026; set `DEMO_TODAY=""` to use the real date |
| Obligations, never "cancel this" advice | rent, emi, loan, insurance, tax, investments | `shared/categories.py` |
| Never propose a limit for | the above + income + health | `shared/categories.py` |
| Healthy savings rate | 20% | `rules.py:savings_rate` |
| Category counts as "spiking" | >20% above the median month **and** >₹1,000 | `rules.py:category_spike` |
| Treated as a one-off, not a habit | one transaction ≥60% of the increase | `rules.py:category_spike` |
| Budget "at risk" | projected > limit; "on track" above 85% of it | `budget_service/main.py:_verdict` |
| Cash runway warning | under 30 days of cover (critical under 15) | `rules.py:cash_runway` |
| Health score weights | 35 / 30 / 20 / 15 | `rules.py:health_score` |
| Chat word aliases ("eating out" → dining) | wording only, resolves against categories in the data | `chat_service/fallback.py:SYNONYMS` |

## Tests

```bash
.venv/bin/python -m pytest        # 60 tests, no network needed
```

`test_analytics.py` covers the maths, `test_rules.py` asserts the house rules
(no insight without a reason, every reason quotes figures, data points trace
back to records, one broken rule can't take the advisor down),
`test_services.py` runs the real seed data through two services over HTTP, and
`test_chat_fallback.py` covers what the advisor answers — and what it refuses
to.

## Configuration

Copy `.env.example` to `.env`. Every value has a working default, so the stack
boots with no `.env` at all. Useful ones:

- `DATA_DIR` — point at another folder to run the whole stack on a different
  book of customers
- `PERSIST_WRITES=true` — let `POST /transactions` write back to the JSON
- `DEMO_TODAY` — the demo clock
- `UPSTREAM_TIMEOUT_SECONDS` — how long a service waits on a dependency

## When a service is down

Stop one (`kill $(cat .run/insight-service.pid)`) and the estate degrades
honestly: `/health/system` marks it down and the header strip in the UI turns
that cell red, calls that need it return `503` with a readable message and a
hint naming the URL that failed, and the advisor says it cannot reach your
data rather than answering without it. Nothing fabricates a fallback number.
