// Money formatting. Every rupee figure in the UI comes through here, so
// grouping is lakh/crore everywhere (₹1,45,000 — not ₹145,000) and a debit
// always reads the same way.

const grouper = (decimals) =>
  new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: decimals ? 2 : 0,
    maximumFractionDigits: decimals ? 2 : 0,
  });

export function formatINR(amount, { decimals = false, signed = false } = {}) {
  const value = Number(amount) || 0;
  const sign = value < 0 ? "−" : signed ? "+" : "";
  return `${sign}₹${grouper(decimals).format(Math.abs(value))}`;
}

// For the bank console, where eight balances share one row.
export function formatINRShort(amount) {
  const value = Math.abs(Number(amount) || 0);
  const sign = Number(amount) < 0 ? "−" : "";
  if (value >= 10000000) return `${sign}₹${(value / 10000000).toFixed(2)}Cr`;
  if (value >= 100000) return `${sign}₹${(value / 100000).toFixed(2)}L`;
  return formatINR(amount);
}

export function formatDate(iso) {
  if (!iso) return "";
  const date = new Date(`${String(iso).slice(0, 10)}T00:00:00`);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
}

export const percent = (value) => `${Math.round(Number(value) || 0)}%`;
