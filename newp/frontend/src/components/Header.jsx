import SystemStatus from "./SystemStatus";

const TABS = ["overview", "transactions", "budgets", "insights", "advisor", "bank"];

export default function Header({ customers, activeCustomer, onSwitch, activeTab, onTabChange }) {
  return (
    <header className="header">
      <div className="header-row box">
        <div className="brand">
          <span className="brand-mark mono">PFA</span>
          <span className="brand-tag">/ ledger</span>
        </div>

        <nav className="tabs">
          {TABS.map((tab) => (
            <button
              key={tab}
              className={`tab-btn ${activeTab === tab ? "tab-btn--active" : ""}`}
              onClick={() => onTabChange(tab)}
            >
              {tab}
            </button>
          ))}
        </nav>

        <div className="customer-switch">
          <span className="eyebrow">{activeTab === "bank" ? "advising" : "viewing"}</span>
          <select
            className="customer-select mono"
            value={activeCustomer ?? ""}
            onChange={(e) => onSwitch(e.target.value)}
          >
            {customers.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      <SystemStatus />
    </header>
  );
}
