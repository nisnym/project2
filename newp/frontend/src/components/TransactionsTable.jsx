import { formatINR, formatDate } from "../format";

export default function TransactionsTable({ transactions }) {
  const sorted = [...transactions].sort((a, b) => (a.date < b.date ? 1 : -1));

  return (
    <div className="ledger-table box">
      <div className="ledger-row ledger-row--head eyebrow">
        <span>date</span>
        <span>merchant</span>
        <span>category</span>
        <span className="ledger-amount-col">amount</span>
      </div>

      {sorted.length === 0 && (
        <div className="ledger-empty">
          No transactions in range. Nothing has posted yet for this window.
        </div>
      )}

      {sorted.map((t) => (
        <div className="ledger-row" key={t.id}>
          <span className="mono ledger-date">{formatDate(t.date)}</span>
          <span className="ledger-merchant">
            {t.merchant}
            {t.note && <span className="ledger-note"> — {t.note}</span>}
          </span>
          <span className="ledger-category">
            <span className="category-chip">{t.category}</span>
          </span>
          <span
            className={`mono ledger-amount-col ${
              t.amount < 0 ? "amount-debit" : "amount-credit"
            }`}
          >
            {formatINR(t.amount, { decimals: true, signed: true })}
          </span>
        </div>
      ))}
    </div>
  );
}
