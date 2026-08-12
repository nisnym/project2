// Thin API client. Everything goes through the gateway on :8080, which fans
// out to the five FastAPI services behind it.
//
// Read calls fall back to bundled mock data if the gateway can't be reached,
// so the UI is never a blank screen — but the status strip in the header says
// plainly when that is happening. Writes never fall back: pretending a
// transaction posted when it didn't would be worse than an error.

import { CUSTOMERS, ACCOUNTS, TRANSACTIONS, BUDGETS, INSIGHTS, HEALTH } from "./mockData";

const BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8080";

async function request(path, options) {
  const res = await fetch(`${BASE_URL}${path}`, options);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const error = body?.error ?? {};
    throw new Error(error.message || `Request failed (${res.status})`);
  }
  return body;
}

async function safeFetch(path, options) {
  try {
    return await request(path, options);
  } catch {
    return null; // caller substitutes mock data
  }
}

// --- reads -------------------------------------------------------------

export async function getSystemHealth() {
  return safeFetch("/health/system");
}

export async function getCustomers() {
  return (await safeFetch("/customers")) || CUSTOMERS;
}

export async function getAccount(customerId) {
  return (await safeFetch(`/customers/${customerId}/account`)) || ACCOUNTS[customerId] || null;
}

export async function getTransactions(customerId) {
  return (await safeFetch(`/customers/${customerId}/transactions`)) || TRANSACTIONS[customerId] || [];
}

export async function getBudgets(customerId) {
  return (await safeFetch(`/customers/${customerId}/budgets`)) || BUDGETS[customerId] || [];
}

export async function getInsights(customerId) {
  return (await safeFetch(`/customers/${customerId}/insights`)) || INSIGHTS[customerId] || [];
}

export async function getHealthScore(customerId) {
  return (await safeFetch(`/customers/${customerId}/health-score`)) || HEALTH[customerId] || null;
}

export async function getBudgetSuggestions(customerId) {
  return (await safeFetch(`/customers/${customerId}/budgets/suggestions`)) || [];
}

// Month-to-date totals. The month itself is the backend's business (it runs on
// a pinned demo clock), so the UI never does its own calendar maths.
export async function getSummary(customerId) {
  return safeFetch(`/customers/${customerId}/summary`);
}

// --- bank-side ---------------------------------------------------------

export async function getBankSummary() {
  return safeFetch("/bank/summary");
}

export async function getBankPortfolio() {
  return (await safeFetch("/bank/portfolio")) || [];
}

export async function getBankAlerts() {
  return (await safeFetch("/bank/alerts")) || [];
}

export async function getBankReview(customerId) {
  return safeFetch(`/bank/customers/${customerId}/review`);
}

// --- writes (no silent fallback) ---------------------------------------

export async function addTransaction(customerId, transaction) {
  return request(`/customers/${customerId}/transactions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(transaction),
  });
}

export async function setBudgetLimit(customerId, category, monthlyLimit) {
  return request(`/customers/${customerId}/budgets/${category}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ monthlyLimit }),
  });
}

// --- advisor -----------------------------------------------------------

export async function sendChatMessage(customerId, message, history = []) {
  const data = await safeFetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ customerId, message, history }),
  });

  if (data) return data;

  return {
    answer:
      "I can't reach your account data right now, so I won't guess. Start the backend " +
      "(./start.sh in backend/) and ask me again.",
    reasoning: "The gateway on :8080 did not respond.",
    data_points_used: [],
    confidence: "low",
    intent: "unavailable",
    source: "offline",
  };
}
