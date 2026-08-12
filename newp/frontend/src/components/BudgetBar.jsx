import { formatINR } from "../format";

// The bar shows two things at once: the solid fill is what has actually been
// spent, the dashed tick is where the month is projected to finish. A budget
// can be inside its limit today and still be marked at-risk, and the reason
// underneath says why in words.
export default function BudgetBar({ category, monthlyLimit, spentSoFar, projectedSpend, status, reason }) {
  const spentPct = monthlyLimit ? Math.min((spentSoFar / monthlyLimit) * 100, 100) : 0;
  const projectedPct = monthlyLimit
    ? Math.min(((projectedSpend ?? spentSoFar) / monthlyLimit) * 100, 100)
    : 0;
  const over = status === "over" || spentSoFar > monthlyLimit;
  const remaining = monthlyLimit - spentSoFar;
  const showProjection = (projectedSpend ?? 0) > spentSoFar && projectedPct > spentPct + 1;

  return (
    <div className="budget-row box">
      <div className="budget-head">
        <span className="budget-category">{category}</span>
        <span className={`budget-status budget-status--${status ?? (over ? "over" : "under")}`}>
          {status ?? (over ? "over" : "under")}
        </span>
      </div>

      <div className="budget-figures-row">
        <span className={`mono budget-figures ${over ? "amount-debit" : "amount-credit"}`}>
          {formatINR(spentSoFar)} / {formatINR(monthlyLimit)}
        </span>
        <span className={`budget-note mono ${over ? "amount-debit" : "text-muted"}`}>
          {over ? `over by ${formatINR(Math.abs(remaining))}` : `${formatINR(remaining)} left`}
        </span>
      </div>

      <div className="budget-track">
        <div
          className={`budget-fill ${over ? "budget-fill--over" : ""}`}
          style={{ width: `${spentPct}%` }}
        />
        {showProjection && (
          <div
            className="budget-projection"
            style={{ left: `${projectedPct}%` }}
            title={`Projected month-end: ${formatINR(projectedSpend)}`}
          />
        )}
      </div>

      {showProjection && (
        <span className="budget-projected mono text-muted">
          projected {formatINR(projectedSpend)} by month end
        </span>
      )}

      {reason && <p className="budget-reason">{reason}</p>}
    </div>
  );
}
