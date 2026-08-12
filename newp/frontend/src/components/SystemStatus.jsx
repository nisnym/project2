import { useEffect, useState } from "react";
import { getSystemHealth } from "../api";

const SERVICE_LABELS = {
  "customer-service": "customer",
  "transaction-service": "transaction",
  "budget-service": "budget",
  "insight-service": "insight",
  "chat-service": "chat",
};

// Live proof that every layer is actually running. Polls the gateway, which
// probes the five services behind it — kill one in a terminal and its cell
// turns red here within five seconds.
export default function SystemStatus({ onStatusChange }) {
  const [health, setHealth] = useState(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    let alive = true;

    async function poll() {
      const data = await getSystemHealth();
      if (!alive) return;
      setHealth(data);
      setChecked(true);
      onStatusChange?.(Boolean(data));
    }

    poll();
    const timer = setInterval(poll, 5000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [onStatusChange]);

  const offline = checked && !health;

  return (
    <div className="status-strip box">
      <span className="eyebrow">services</span>

      <span className={`status-cell ${offline ? "status-cell--down" : "status-cell--up"}`}>
        <i className={`status-dot ${offline ? "status-dot--down" : "status-dot--up"}`} />
        <span className="mono">gateway</span>
      </span>

      {offline && (
        <span className="status-offline mono">
          not reachable on :8080 — showing sample data
        </span>
      )}

      {(health?.services ?? []).map((service) => (
        <span
          key={service.name}
          className={`status-cell ${service.status === "up" ? "status-cell--up" : "status-cell--down"}`}
          title={service.detail || `${service.url} · ${service.latencyMs}ms`}
        >
          <i className={`status-dot status-dot--${service.status}`} />
          <span className="mono">{SERVICE_LABELS[service.name] ?? service.name}</span>
          {service.status === "up" && service.latencyMs != null && (
            <span className="status-latency mono">{Math.round(service.latencyMs)}ms</span>
          )}
        </span>
      ))}

      {health && (
        <span className="status-clock mono">{health.checkedAt?.slice(11)}</span>
      )}
    </div>
  );
}
