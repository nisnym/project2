import { formatINR } from "../format";

// Four weighted components, each with the sentence that justifies its score.
// The number is never shown on its own — a score without its reasoning is
// exactly the kind of thing a customer can't argue with or act on.
export default function HealthPanel({ health, account }) {
  if (!health) return null;

  return (
    <div className="health-panel box">
      <div className="health-head">
        <div className="health-score-block">
          <span className="eyebrow">financial health</span>
          <span className={`health-score mono health-score--${health.band}`}>{health.score}</span>
          <span className="health-outof mono">/100</span>
        </div>
        <div className="health-meta">
          <span className={`band-tag band-tag--${health.band}`}>{health.band}</span>
          {account && (
            <span className="health-balance mono">{formatINR(account.balance)} on hand</span>
          )}
        </div>
      </div>

      <div className="health-components">
        {(health.components ?? []).map((component) => (
          <div className="health-component" key={component.label}>
            <div className="health-component-head">
              <span className="health-component-label">{component.label}</span>
              <span className="mono health-component-score">
                {component.score}
                <span className="health-weight"> · {Math.round(component.weight * 100)}%</span>
              </span>
            </div>
            <div className="health-track">
              <div
                className={`health-fill ${component.score < 45 ? "health-fill--low" : ""}`}
                style={{ width: `${component.score}%` }}
              />
            </div>
            <p className="health-reason">{component.reason}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
