# frontend — PFA / ledger

React + Vite SPA. Two journeys over the same data: the customer's app
(overview, transactions, budgets, findings, advisor) and the bank console
(portfolio, alert queue, customer review).

## Design

**Ledger / bank-statement aesthetic** — every element lives inside a hard,
2px-bordered box with zero border-radius; boxes share edges rather than
floating as cards, so the page reads as one continuous statement rather than a
dashboard. All money renders in JetBrains Mono with tabular numerals and
lakh/crore grouping (₹1,45,000, never ₹145,000), colour-coded green for
credit/under and coral for debit/over — that consistent ledger typography is
the page's signature. Inter carries labels and body copy. Accent is a single
banknote gold, used only for active states, projections and the send button.

Tokens live in `src/styles/tokens.css`. Money formatting lives in
`src/format.js` — nothing formats a rupee figure inline.

## Setup

```bash
npm install
npm run dev          # http://localhost:5173
```

The backend is expected on `http://localhost:8080`. Override with
`VITE_API_BASE_URL` in `.env` (see `.env.example`). From the repo root,
`./start.sh` boots the backend and this together.

## Backend not running?

Every **read** falls back to bundled sample data (`src/mockData.js`), so the UI
never shows a broken screen — and the status strip under the header says
"not reachable" while that's happening, so nobody mistakes sample data for
live data. **Writes** (posting a transaction, applying a budget limit) never
fall back; they surface the error instead, because pretending a write
succeeded is worse than failing.

## Structure

```
src/
  App.jsx                     # shell, tab routing, data loading, refresh
  api.js                      # gateway client, mock fallback on reads only
  format.js                   # ₹ formatting, Indian grouping, dates
  mockData.js                 # offline sample data
  components/
    Header.jsx                # brand, tabs, customer switcher
    SystemStatus.jsx          # live per-service health, polled every 5s
    StatBox.jsx               # overview stat cell
    TransactionsTable.jsx     # the ledger table
    AddTransaction.jsx        # post a spend and watch everything move
    BudgetBar.jsx             # spend bar + dashed projection tick + reason
    BudgetSuggestions.jsx     # proposed limits, each with its reason, appliable
    InsightCard.jsx           # finding → why → what to do → traced to
    HealthPanel.jsx           # score with its four justified components
    ChatPanel.jsx             # advisor, reasoning and data points inline
    BankConsole.jsx           # portfolio, review, alert queue
  styles/
    tokens.css                # colour / type / spacing
    global.css                # resets, .box/.mono/.eyebrow utilities
    layout.css                # component + responsive layout
```

## What each tab talks to

| Tab | Gateway routes |
|---|---|
| overview | `/customers/{id}/{account,transactions,budgets,insights,health-score,summary}` |
| transactions | the above + `POST /customers/{id}/transactions` |
| budgets | `/customers/{id}/budgets`, `/budgets/suggestions`, `PUT /budgets/{category}` |
| insights | `/customers/{id}/insights` |
| advisor | `POST /chat` |
| bank | `/bank/summary`, `/bank/portfolio`, `/bank/alerts`, `/bank/customers/{id}/review` |
| header strip | `/health/system` every 5s |

## Build

```bash
npm run build       # dist/
npm run preview
npx oxlint
```
