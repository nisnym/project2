import { useEffect, useState } from "react";
import { getBankAlerts, getBankPortfolio, getBankReview, getBankSummary } from "../api";
import { formatINR, formatINRShort, percent } from "../format";
import StatBox from "./StatBox";

// The bank-side journey, over the same ledger the customer sees. Nothing here
// is a second dataset: the portfolio is the insight engine run across every
// customer, so a transaction posted on the customer screen changes this one.
export default function BankConsole({ onOpenCustomer }) {
  const [summary, setSummary] = useState(null);
  const [portfolio, setPortfolio] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [selected, setSelected] = useState(null);
  const [review, setReview] = useState(null);

  useEffect(() => {
    getBankSummary().then(setSummary);
    getBankPortfolio().then(setPortfolio);
    getBankAlerts().then(setAlerts);
  }, []);

  useEffect(() => {
    if (!selected) {
      setReview(null);
      return;
    }
    setReview(null);
    getBankReview(selected).then(setReview);
  }, [selected]);

  return (
    <div className="bank">
      <div className="stat-row">
        <StatBox label="customers" value={summary?.customers ?? "—"} sublabel="in this book" />
        <StatBox
          label="needing attention"
          value={summary?.customersNeedingAttention ?? "—"}
          tone={summary?.customersNeedingAttention ? "debit" : "credit"}
          sublabel={`${summary?.criticalAlerts ?? 0} critical`}
        />
        <StatBox
          label="deposits held"
          value={summary ? formatINRShort(summary.totalBalance) : "—"}
        />
        <StatBox
          label="avg health"
          value={summary?.averageHealthScore ?? "—"}
          sublabel="0–100, weighted"
        />
      </div>

      <div className="section-title">
        <h2>Portfolio</h2>
        <span className="eyebrow">worst health first · click a row to open the review</span>
      </div>

      <div className="portfolio box">
        <div className="portfolio-row portfolio-row--head eyebrow">
          <span>customer</span>
          <span>segment</span>
          <span className="num">balance</span>
          <span className="num">spent this month</span>
          <span className="num">saves</span>
          <span className="num">health</span>
          <span>top flag</span>
        </div>

        {portfolio.length === 0 && (
          <div className="ledger-empty">No customers loaded. Is the gateway running?</div>
        )}

        {portfolio.map((row) => (
          <button
            key={row.customerId}
            className={`portfolio-row ${selected === row.customerId ? "portfolio-row--active" : ""}`}
            onClick={() => setSelected(selected === row.customerId ? null : row.customerId)}
          >
            <span className="portfolio-name">{row.name}</span>
            <span className="category-chip">{row.segment}</span>
            <span className="num mono">{formatINR(row.balance)}</span>
            <span className="num mono amount-debit">{formatINR(row.monthToDateSpend)}</span>
            <span className={`num mono ${row.savingsRate >= 20 ? "amount-credit" : "amount-debit"}`}>
              {percent(row.savingsRate)}
            </span>
            <span className={`num mono band-score band-score--${row.band}`}>{row.healthScore}</span>
            <span className="portfolio-flag">
              {row.openAlerts > 0 && <span className="alert-count mono">{row.openAlerts}</span>}
              {row.topFlag ?? "nothing outstanding"}
            </span>
          </button>
        ))}
      </div>

      {selected && (
        <>
          <div className="section-title">
            <h2>Review · {portfolio.find((r) => r.customerId === selected)?.name}</h2>
            <button className="link-btn mono" onClick={() => onOpenCustomer?.(selected)}>
              open their app view →
            </button>
          </div>

          {!review && <div className="ledger-empty box">Pulling the file…</div>}

          {review && (
            <div className="review-grid">
              <div className="review-col">
                <div className="talking-points box">
                  <div className="chat-header eyebrow">talking points</div>
                  {review.talkingPoints.map((point, i) => (
                    <div className="talking-point" key={i}>
                      <span className={`severity-tag severity-tag--${point.severity}`}>
                        {point.severity}
                      </span>
                      <h4 className="talking-headline">{point.headline}</h4>
                      <p className="talking-reason">{point.reason}</p>
                      {point.action && <p className="talking-action">{point.action}</p>}
                    </div>
                  ))}
                  {review.talkingPoints.length === 0 && (
                    <div className="ledger-empty">Nothing to raise with this customer.</div>
                  )}
                </div>
              </div>

              <div className="review-col">
                <div className="trend box">
                  <div className="chat-header eyebrow">spend vs income</div>
                  <div className="trend-rows">
                    {review.trend
                      .filter((m) => m.count > 0)
                      .map((month) => {
                        const peak = Math.max(
                          ...review.trend.map((t) => Math.max(t.spend, t.income)),
                          1,
                        );
                        return (
                          <div className="trend-row" key={month.month}>
                            <span className="mono trend-month">{month.month}</span>
                            <div className="trend-bars">
                              <div
                                className="trend-bar trend-bar--income"
                                style={{ width: `${(month.income / peak) * 100}%` }}
                              />
                              <div
                                className="trend-bar trend-bar--spend"
                                style={{ width: `${(month.spend / peak) * 100}%` }}
                              />
                            </div>
                            <span className={`mono trend-net ${month.net >= 0 ? "amount-credit" : "amount-debit"}`}>
                              {formatINR(month.net, { signed: true })}
                            </span>
                          </div>
                        );
                      })}
                  </div>
                  <div className="trend-legend eyebrow">
                    <span className="legend-swatch legend-swatch--income" /> income
                    <span className="legend-swatch legend-swatch--spend" /> spend
                  </div>
                </div>

                {review.suggestions?.length > 0 && (
                  <div className="proposals box">
                    <div className="chat-header eyebrow">limits worth proposing</div>
                    {review.suggestions.map((s) => (
                      <div className="proposal" key={s.category}>
                        <div className="proposal-head">
                          <span className="suggestion-category">{s.category}</span>
                          <span className="mono">
                            {s.currentLimit != null ? `${formatINR(s.currentLimit)} → ` : ""}
                            {formatINR(s.suggestedLimit)}
                          </span>
                        </div>
                        <p className="proposal-reason">{s.reason}</p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </>
      )}

      <div className="section-title">
        <h2>Alert queue</h2>
        <span className="eyebrow">across the whole book · worst first</span>
      </div>

      <div className="alert-queue">
        {alerts.length === 0 && <div className="ledger-empty box">No open alerts.</div>}
        {alerts.map((alert, i) => (
          <div className={`alert box alert--${alert.severity}`} key={i}>
            <div className="alert-head">
              <span className={`severity-tag severity-tag--${alert.severity}`}>{alert.severity}</span>
              <button className="alert-customer link-btn" onClick={() => setSelected(alert.customerId)}>
                {alert.customerName}
              </button>
              {alert.impact ? <span className="mono alert-impact">{formatINR(alert.impact)}</span> : null}
            </div>
            <h4 className="alert-headline">{alert.headline}</h4>
            <p className="alert-reason">{alert.reason}</p>
            {alert.recommendedAction && <p className="alert-action">{alert.recommendedAction}</p>}
          </div>
        ))}
      </div>
    </div>
  );
}
