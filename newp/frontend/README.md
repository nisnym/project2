# frontend — PFA / ledger

React + Vite SPA for the customer journey: account overview, transactions,
budgets, and the AI advisor chat. Built to run standalone against mock data
until the Java `api-gateway` is reachable, then switches over automatically.

## Design

**Ledger / bank-statement aesthetic** — every element lives inside a hard,
2px-bordered box with zero border-radius; boxes share edges rather than
floating as cards, so the page reads as one continuous statement rather than
a dashboard. All monetary figures render in JetBrains Mono with tabular
numerals and are color-coded (green = credit/under budget, coral =
debit/over budget) — that consistent ledger typography is the page's
signature element. Inter carries labels and body copy. Accent color is a
single banknote gold, used only for active states and the send button.

Tokens live in `src/styles/tokens.css` if you want to retheme.

## Setup

```bash
cd frontend
npm install
cp .env.example .env      # point VITE_API_BASE_URL at your api-gateway
npm run dev
```

Opens at http://localhost:5173

## Backend not running yet?

No problem — every API call in `src/api.js` falls back to the bundled
mock data (`src/mockData.js`, same shape as the real contract) if the
fetch fails. The UI is fully demoable with zero backend running.

## Build

```bash
npm run build       # outputs to dist/
npm run preview     # serve the production build locally
```

## Structure

```
src/
  App.jsx                     # page shell, tab routing, data loading
  api.js                       # fetch client w/ mock fallback
  mockData.js                   # offline demo data
  components/
    Header.jsx                  # brand, tab nav, customer switcher
    StatBox.jsx                  # overview stat cell
    TransactionsTable.jsx         # the ledger table
    BudgetBar.jsx                  # segmented budget progress
    ChatPanel.jsx                   # AI advisor chat, reasoning shown inline
  styles/
    tokens.css                  # design tokens (color/type/spacing)
    global.css                   # resets, .box/.mono/.eyebrow utilities
    layout.css                    # component + responsive layout
```

## Wiring to the real backend

Point `VITE_API_BASE_URL` in `.env` at your `api-gateway`. Expected routes
(adjust `src/api.js` if your gateway uses different paths):

- `GET /customers`
- `GET /customers/{id}/account`
- `GET /customers/{id}/transactions`
- `GET /customers/{id}/budgets`
- `POST /chat` — body: `{ customer, transactions, budgets, message }`,
  same contract as `ai-service`'s `/chat` endpoint.
