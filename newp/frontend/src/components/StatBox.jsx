export default function StatBox({ label, value, sublabel, tone }) {
  const toneClass =
    tone === "credit" ? "amount-credit" : tone === "debit" ? "amount-debit" : "";

  return (
    <div className="stat-box box">
      <span className="eyebrow">{label}</span>
      <span className={`stat-value mono ${toneClass}`}>{value}</span>
      {sublabel && <span className="stat-sublabel">{sublabel}</span>}
    </div>
  );
}
