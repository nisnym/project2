# data/ — the whole book of business

Four JSON files. **No customer fact lives anywhere else in the codebase.**
Change these and the entire product — balances, budgets, insights, health
scores, chat answers, the bank console — describes a different person.

| File | Owned by | Shape |
|---|---|---|
| `customers.json` | customer-service | `id, name, segment, city, monthlyIncome, incomeStability, goals[]` |
| `accounts.json` | customer-service | `id, customerId, type, balance, currency` |
| `transactions.json` | transaction-service | `id, customerId, date, amount, category, merchant, channel, note` |
| `budgets.json` | budget-service | `customerId, category, monthlyLimit` |

## Rules the code relies on

- **Amount sign is the only thing that classifies a transaction.** Negative is
  money out, positive is money in. There is no whitelist of "spend"
  categories anywhere.
- **Categories are free text.** Add `"category": "pet-care"` to a transaction
  and it appears in summaries, budgets and insights with no code change.
- **Dates are ISO `YYYY-MM-DD`.** Month-to-date maths keys off `DEMO_TODAY`
  in `.env` (pinned to `2026-08-12`, the middle of the seeded August).
- **`monthlyLimit` is the only budget input.** `spentSoFar`, `remaining`,
  `pctUsed`, `projectedSpend` and `status` are all derived live from the
  ledger — that is why editing a transaction instantly moves a budget bar.

## Editing live

Services stat the file on every read and reload when the mtime changes, so
you can edit any of these in a text editor and just refresh the browser. No
restart, no rebuild.

## Adding a customer

Add one row to `customers.json`, one to `accounts.json`, some transactions,
and (optionally) budgets. Everything downstream — the customer switcher, the
bank portfolio, the health score, the advisor — picks them up automatically.
A customer with **no** budgets and **no** transactions is a supported state:
the app says it does not have enough history yet, rather than inventing
numbers.

## The three seeded people (and why each exists)

| Customer | Shape of their month | What it exercises |
|---|---|---|
| **Ananya Iyer** — salaried, Bengaluru, ₹1,45,000/mo | Dining already ₹11,900 against an ₹8,000 limit by the 12th; a one-off ₹32,999 laptop | Budget breach, spike detection, one-off vs habit |
| **Rohit Menon** — student, Pune, ₹18,000 stipend | ₹3,180 of food delivery on a ₹2,500 limit, ₹8,420 balance | Low balance risk, small-money advice, thin history |
| **Farhan Qureshi** — self-employed, Delhi, variable | Rent + EMI cleared on the 4th–5th, income only landed on the 12th | Income variability, timing risk, buffer advice |
