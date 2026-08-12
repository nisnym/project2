import { formatINR } from "../format";

// Every insight renders the same three blocks in the same order: what we
// found, why we think it, and what to do. The data points at the bottom are
// the receipts — each one names the record it came from.
export default function InsightCard({ insight }) {
  const { severity, headline, detail, reason, recommendedAction, impact, dataPoints = [] } = insight;

  return (
    <article className={`insight box insight--${severity}`}>
      <header className="insight-head">
        <span className={`severity-tag severity-tag--${severity}`}>{severity}</span>
        {impact ? <span className="insight-impact mono">{formatINR(impact)}</span> : null}
      </header>

      <h3 className="insight-headline">{headline}</h3>
      {detail && <p className="insight-detail mono">{detail}</p>}

      <div className="insight-block">
        <span className="eyebrow">why</span>
        <p className="insight-text">{reason}</p>
      </div>

      {recommendedAction && (
        <div className="insight-block insight-block--action">
          <span className="eyebrow">what to do</span>
          <p className="insight-text">{recommendedAction}</p>
        </div>
      )}

      {dataPoints.length > 0 && (
        <div className="datapoints">
          <span className="eyebrow">traced to</span>
          <ul className="datapoint-list">
            {dataPoints.map((point, i) => (
              <li className="datapoint" key={`${point.label}-${i}`}>
                <span className="datapoint-label">{point.label}</span>
                <span className="datapoint-value mono">{point.value}</span>
                {point.source && <span className="datapoint-source mono">{point.source}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </article>
  );
}
