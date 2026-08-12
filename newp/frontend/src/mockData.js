// Offline fallback only — a trimmed copy of what the gateway returns, in the
// same shape (see backend/data/*.json for the real ledger). Used when the
// backend isn't running so the UI still renders; the status strip in the
// header shows "offline" whenever these are on screen.

export const CUSTOMERS = [
  { id: "cust-001", name: "Ananya Iyer", segment: "salaried", city: "Bengaluru", monthlyIncome: 145000 },
  { id: "cust-002", name: "Rohit Menon", segment: "student", city: "Pune", monthlyIncome: 18000 },
  { id: "cust-003", name: "Farhan Qureshi", segment: "self-employed", city: "Delhi", monthlyIncome: 95000 },
];

export const ACCOUNTS = {
  "cust-001": { id: "acc-001", customerId: "cust-001", type: "savings", balance: 214560.75, currency: "INR" },
  "cust-002": { id: "acc-002", customerId: "cust-002", type: "savings", balance: 8420.5, currency: "INR" },
  "cust-003": { id: "acc-003", customerId: "cust-003", type: "current", balance: 96350.2, currency: "INR" },
};

export const TRANSACTIONS = {
  "cust-001": [
    { id: "t1059", customerId: "cust-001", date: "2026-08-12", amount: -2100, category: "dining", merchant: "Naru Noodle Bar" },
    { id: "t1057", customerId: "cust-001", date: "2026-08-11", amount: -1240, category: "dining", merchant: "Third Wave Coffee" },
    { id: "t1055", customerId: "cust-001", date: "2026-08-09", amount: -32999, category: "shopping", merchant: "Croma" },
    { id: "t1052", customerId: "cust-001", date: "2026-08-08", amount: -2340, category: "dining", merchant: "Zomato" },
    { id: "t1048", customerId: "cust-001", date: "2026-08-05", amount: -4120, category: "groceries", merchant: "BigBasket" },
    { id: "t1043", customerId: "cust-001", date: "2026-08-02", amount: -32000, category: "rent", merchant: "Prestige Apartments" },
    { id: "t1042", customerId: "cust-001", date: "2026-08-01", amount: 145000, category: "income", merchant: "Infosys Ltd" },
  ],
  "cust-002": [
    { id: "t2035", customerId: "cust-002", date: "2026-08-12", amount: -380, category: "dining", merchant: "Swiggy" },
    { id: "t2033", customerId: "cust-002", date: "2026-08-10", amount: -710, category: "dining", merchant: "Swiggy" },
    { id: "t2028", customerId: "cust-002", date: "2026-08-05", amount: -6500, category: "rent", merchant: "Kothrud PG" },
    { id: "t2027", customerId: "cust-002", date: "2026-08-05", amount: 18000, category: "income", merchant: "Persistent Systems" },
  ],
  "cust-003": [
    { id: "t3037", customerId: "cust-003", date: "2026-08-12", amount: 52000, category: "income", merchant: "Upwork Payout" },
    { id: "t3035", customerId: "cust-003", date: "2026-08-10", amount: -3100, category: "dining", merchant: "Swiggy" },
    { id: "t3030", customerId: "cust-003", date: "2026-08-05", amount: -12500, category: "emi", merchant: "HDFC Car Loan" },
    { id: "t3029", customerId: "cust-003", date: "2026-08-04", amount: -28000, category: "rent", merchant: "Green Park Landlord" },
  ],
};

export const BUDGETS = {
  "cust-001": [
    {
      category: "shopping", monthlyLimit: 6000, spentSoFar: 32999, remaining: -26999,
      pctUsed: 550, status: "over", projectedSpend: 36098,
      reason: "₹32,999 spent against a ₹6,000 limit — ₹26,999 past it, with 19 days still to run in August 2026.",
    },
    {
      category: "dining", monthlyLimit: 8000, spentSoFar: 11900, remaining: -3900,
      pctUsed: 148.8, status: "over", projectedSpend: 13865,
      reason: "₹11,900 spent against a ₹8,000 limit — ₹3,900 past it, with 19 days still to run in August 2026.",
    },
    {
      category: "groceries", monthlyLimit: 12000, spentSoFar: 5300, remaining: 6700,
      pctUsed: 44.2, status: "under", projectedSpend: 10260,
      reason: "₹5,300 of ₹12,000, heading for about ₹10,260. ₹6,700 of headroom.",
    },
  ],
  "cust-002": [
    {
      category: "dining", monthlyLimit: 2500, spentSoFar: 3180, remaining: -680,
      pctUsed: 127.2, status: "over", projectedSpend: 4790,
      reason: "₹3,180 spent against a ₹2,500 limit — ₹680 past it, with 19 days still to run in August 2026.",
    },
  ],
  "cust-003": [
    {
      category: "dining", monthlyLimit: 6000, spentSoFar: 3100, remaining: 2900,
      pctUsed: 51.7, status: "under", projectedSpend: 4850,
      reason: "₹3,100 of ₹6,000, heading for about ₹4,850. ₹2,900 of headroom.",
    },
  ],
};

export const INSIGHTS = {
  "cust-001": [
    {
      id: "budget-breach-dining", customerId: "cust-001", type: "budget_breach", severity: "critical",
      category: "dining", headline: "Dining is ₹3,900 over its limit",
      detail: "₹11,900 spent of an ₹8,000 limit, 12 days into August 2026.",
      reason: "₹11,900 spent against an ₹8,000 limit — ₹3,900 past it, with 19 days still to run in August 2026.",
      recommendedAction: "Spending nothing more on dining still ends the month above plan, so the decision now is which other category gives that back.",
      impact: 3900, confidence: "high",
      dataPoints: [{ label: "Limit", value: "₹8,000", source: "budget:dining" }],
    },
  ],
  "cust-002": [],
  "cust-003": [],
};

export const HEALTH = {
  "cust-001": { customerId: "cust-001", score: 71, band: "steady", summary: "Offline sample.", components: [] },
  "cust-002": { customerId: "cust-002", score: 38, band: "strained", summary: "Offline sample.", components: [] },
  "cust-003": { customerId: "cust-003", score: 55, band: "stretched", summary: "Offline sample.", components: [] },
};
