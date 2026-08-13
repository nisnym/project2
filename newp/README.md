# PFA — a bank that knows you, and says why

A personal financial advisor built on a bank's own ledger. Two journeys over
one set of data: the customer's app, and the advisor's console at the bank.

Every number on screen is derived live from that customer's transactions, and
every recommendation states the reason it exists in language a non-technical
customer would accept — traceable back to the transaction that caused it.

## Run it

```bash
./start.sh
```

That boots six FastAPI services, waits for their health checks, then serves
the app at **http://localhost:5173**. Ctrl-C stops everything. First run
installs dependencies (~1 min); after that it's a few seconds.

Nothing else is needed — no database, no API keys, no accounts. The advisor
runs on a deterministic engine out of the box; point it at GitHub Models when
you want the model to compose the sentences (see
[backend/README.md](backend/README.md#the-chatbot)).

```
frontend/   React + Vite SPA — the ledger UI, both journeys
backend/    six FastAPI services + the seed ledger
```

## A five-minute demo

1. **Overview** — Ananya Iyer, ₹2,14,561 balance. Two findings are already at
   the top of the page, each with its *why* and the transactions behind it.
2. **The header strip** — six live service cells with response times. Kill one
   in a terminal (`kill $(cat backend/.run/insight-service.pid)`) and watch its
   cell go red, then bring it back with `backend/start.sh`.
3. **Budgets** — the solid bar is what's spent, the dashed tick is where the
   month is projected to land. Note that *utilities* is at 96% of its limit and
   still calm: both bills already cleared, so nothing more is expected. A
   run-rate would have cried wolf.
4. **Ask the advisor** "how much did I spend on dining?" — it answers with the
   figure, the limit it broke, the reasoning, and the transaction ids. Then ask
   it something it cannot know ("what's the weather in Goa?") and it declines
   instead of improvising.
5. **Transactions → post a spend** (`Toit Brewpub`, `dining`, `2400`). Go back
   to Budgets: the bar, the projection, the insight, the health score and the
   advisor's next answer have all moved. Nothing was precomputed.
6. **Switch customer to Rohit Menon** (student, ₹18,000 stipend) — different
   person, different advice: food delivery over a small limit, 21 days of cash
   cover, thin history handled honestly. Then **Farhan Qureshi**
   (self-employed): his rent and EMI cleared eight days before his invoice
   landed, and that timing is the finding.
7. **Bank tab** — the same ledger read across the whole book: portfolio sorted
   by who needs attention, an alert queue with reasons, and a review screen
   with talking points an advisor could read aloud.

## Change the data, change the product

There is no customer knowledge anywhere in the code. It all lives in four JSON
files in [`backend/data/`](backend/data/README.md):

```bash
$EDITOR backend/data/transactions.json   # edit an amount
```

Refresh the browser. Services re-read the file when its mtime changes, so
balances, budgets, projections, insights, the health score, the bank portfolio
and the advisor's answers all move together. Add a customer by adding one row
to `customers.json` — the switcher, the portfolio and the advisor pick them up
with no code change. A customer with no history is a supported state: the app
says it doesn't have enough to advise on yet, rather than inventing a trend.

The judgements that *are* in code — the pinned demo date, what counts as a
spike, which categories are obligations rather than choices — are listed in one
table in [backend/README.md](backend/README.md#what-is-hardcoded-and-where).

## How it hangs together

```
React SPA ──► gateway :8080 ──┬──► customer-service    :9001   who they are
                              ├──► transaction-service :9002   the ledger + all maths
                              ├──► budget-service      :9003   limits, live spend, pace
                              ├──► insight-service     :9004   advice, each with a reason
                              └──► chat-service        :9005   the advisor (GitHub Models)
```

The gateway holds no data and makes no decisions. `budget`, `insight` and
`chat` hold no data either — a budget's "spent so far" is fetched from the
transaction service on every request, which is why a budget can never drift
out of step with the ledger.

## Why the advice is defensible

- **Compared against the person, not a benchmark.** "93% above your normal"
  where *normal* is the median of their own completed months — a median, so one
  ₹32,999 laptop cannot redefine what typical looks like.
- **A one-off is called a one-off.** When a single purchase accounts for most
  of a category's increase, the product says so and stays calm, instead of
  extrapolating a laptop into a habit.
- **Projections that respect how bills actually work.** See *utilities* above.
- **Goals funded in order, out of one pot.** Three goals are not each "on
  track" against the same rupees; nearer deadlines claim their share first and
  the rest is told what's actually left.
- **It declines.** Out of scope, category it has never seen, not enough
  history, a service that's down — each has its own honest answer. None of them
  is a guess.

## Tests

```bash
cd backend && .venv/bin/python -m pytest    # 60 tests, no network
cd frontend && npm run build && npx oxlint
```

## A note on the stack

The brief calls for three layers with a Java service among them. This is
FastAPI end to end, as asked for in this repo. The gateway is the natural seam
if a Java layer is required: it owns no data and no business rules — it
composes HTTP calls — so it can be replaced by a Spring service speaking the
same routes without touching the five services behind it.
