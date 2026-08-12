import { useState } from "react";
import { setBudgetLimit } from "../api";
import { formatINR } from "../format";

// Suggestions are derived from the customer's own median month, and every one
// of them ships with the sentence that justifies it. Applying one writes the
// new limit back through the gateway and the budget re-evaluates immediately.
export default function BudgetSuggestions({ customerId, suggestions, onApplied }) {
  const [applying, setApplying] = useState(null);
  const [error, setError] = useState(null);

  if (!suggestions?.length) return null;

  async function apply(suggestion) {
    setApplying(suggestion.category);
    setError(null);
    try {
      await setBudgetLimit(customerId, suggestion.category, suggestion.suggestedLimit);
      onApplied?.(suggestion);
    } catch (err) {
      setError(err.message);
    } finally {
      setApplying(null);
    }
  }

  return (
    <div className="suggestions">
      {suggestions.map((suggestion) => (
        <div className="suggestion box" key={suggestion.category}>
          <div className="suggestion-head">
            <span className="suggestion-category">{suggestion.category}</span>
            <span className="suggestion-move mono">
              {suggestion.currentLimit != null && (
                <span className="suggestion-from">{formatINR(suggestion.currentLimit)}</span>
              )}
              <span className="suggestion-arrow">→</span>
              <span className="suggestion-to">{formatINR(suggestion.suggestedLimit)}</span>
            </span>
          </div>

          <p className="suggestion-reason">{suggestion.reason}</p>

          <div className="suggestion-foot">
            <span className={`confidence-tag confidence-tag--${suggestion.confidence}`}>
              {suggestion.confidence} confidence
            </span>
            <button
              className="suggestion-apply"
              onClick={() => apply(suggestion)}
              disabled={applying === suggestion.category}
            >
              {applying === suggestion.category ? "applying…" : "apply this limit"}
            </button>
          </div>

          {suggestion.dataPoints?.length > 0 && (
            <div className="datapoint-list datapoint-list--inline">
              {suggestion.dataPoints.map((point, i) => (
                <span className="datapoint datapoint--inline" key={i}>
                  <span className="datapoint-label">{point.label}</span>
                  <span className="datapoint-value mono">{point.value}</span>
                </span>
              ))}
            </div>
          )}
        </div>
      ))}

      {error && <p className="suggestion-error mono">{error}</p>}
    </div>
  );
}
