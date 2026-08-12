import { useCallback, useEffect, useState } from "react";
import Header from "./components/Header";
import StatBox from "./components/StatBox";
import TransactionsTable from "./components/TransactionsTable";
import BudgetBar from "./components/BudgetBar";
import BudgetSuggestions from "./components/BudgetSuggestions";
import ChatPanel from "./components/ChatPanel";
import InsightCard from "./components/InsightCard";
import HealthPanel from "./components/HealthPanel";
import AddTransaction from "./components/AddTransaction";
import BankConsole from "./components/BankConsole";
import { formatINR } from "./format";
import {
  getAccount,
  getBudgetSuggestions,
  getBudgets,
  getCustomers,
  getHealthScore,
  getInsights,
  getSummary,
  getTransactions,
} from "./api";

const EMPTY = {
  account: null,
  transactions: [],
  budgets: [],
  insights: [],
  health: null,
  summary: null,
  suggestions: [],
};

export default function App() {
  const [customers, setCustomers] = useState([]);
  const [activeCustomer, setActiveCustomer] = useState(null);
  const [activeTab, setActiveTab] = useState("overview");
  const [data, setData] = useState(EMPTY);
  const [refreshKey, setRefreshKey] = useState(0);

  // Anything that changes the ledger bumps this, and every derived figure on
  // screen is refetched. Nothing is cached client-side, so what you see is
  // always what the services just computed.
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  useEffect(() => {
    getCustomers().then((list) => {
      setCustomers(list);
      setActiveCustomer((current) => current ?? (list.length ? list[0].id : null));
    });
  }, []);

  useEffect(() => {
    if (!activeCustomer) return;
    let alive = true;

    Promise.all([
      getAccount(activeCustomer),
      getTransactions(activeCustomer),
      getBudgets(activeCustomer),
      getInsights(activeCustomer),
      getHealthScore(activeCustomer),
      getSummary(activeCustomer),
      getBudgetSuggestions(activeCustomer),
    ]).then(([account, transactions, budgets, insights, health, summary, suggestions]) => {
      if (!alive) return;
      setData({ account, transactions, budgets, insights, health, summary, suggestions });
    });

    return () => {
      alive = false;
    };
  }, [activeCustomer, refreshKey]);

  const { account, transactions, budgets, insights, health, summary, suggestions } = data;

  // Prefer the backend's month-to-date figures; fall back to the ledger we
  // have if the gateway is down and we're on mock data.
  const monthSpend =
    summary?.totalSpend ??
    transactions.filter((t) => t.amount < 0).reduce((sum, t) => sum + Math.abs(t.amount), 0);

  const topCategory = summary?.byCategory?.[0]?.category ?? "—";
  const overBudget = budgets.filter((b) => b.status === "over").length;
  const atRisk = budgets.filter((b) => b.status === "at-risk").length;
  const attention = insights.filter((i) => i.severity === "critical" || i.severity === "warning");
  const customerName = customers.find((c) => c.id === activeCustomer)?.name;

  function openCustomer(customerId) {
    setActiveCustomer(customerId);
    setActiveTab("overview");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  if (!activeCustomer) {
    return (
      <div className="app">
        <div className="app-body">
          <p className="text-muted">Loading ledger…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <Header
        customers={customers}
        activeCustomer={activeCustomer}
        onSwitch={setActiveCustomer}
        activeTab={activeTab}
        onTabChange={setActiveTab}
      />

      <div className="app-body">
        {activeTab !== "bank" && (
          <div className="stat-row">
            <StatBox
              label="balance"
              value={account ? formatINR(account.balance) : "—"}
              sublabel={account ? `${account.type} · ${account.id}` : undefined}
            />
            <StatBox
              label={summary ? `spent in ${summary.month}` : "spent this month"}
              value={formatINR(monthSpend)}
              tone="debit"
              sublabel={summary ? `${summary.daysElapsed} days in` : undefined}
            />
            <StatBox label="top category" value={topCategory} sublabel="by spend" />
            <StatBox
              label="budgets over limit"
              value={overBudget}
              tone={overBudget > 0 ? "debit" : "credit"}
              sublabel={atRisk ? `${atRisk} more at risk` : "none at risk"}
            />
          </div>
        )}

        {activeTab === "overview" && (
          <div className="main-grid">
            <div>
              {attention.length > 0 && (
                <>
                  <div className="section-title">
                    <h2>Needs attention</h2>
                    <button className="link-btn mono" onClick={() => setActiveTab("insights")}>
                      all {insights.length} findings →
                    </button>
                  </div>
                  <div className="insight-list">
                    {attention.slice(0, 2).map((insight) => (
                      <InsightCard key={insight.id} insight={insight} />
                    ))}
                  </div>
                </>
              )}

              <div className="section-title">
                <h2>Recent activity</h2>
                <button className="link-btn mono" onClick={() => setActiveTab("transactions")}>
                  all {transactions.length} transactions →
                </button>
              </div>
              <TransactionsTable transactions={transactions.slice(0, 6)} />

              <div className="section-title">
                <h2>Budgets</h2>
              </div>
              <div className="budget-grid">
                {budgets.slice(0, 4).map((b) => (
                  <BudgetBar key={b.category} {...b} />
                ))}
              </div>
            </div>

            <div className="side-col">
              <HealthPanel health={health} account={account} />
              <ChatPanel customerId={activeCustomer} />
            </div>
          </div>
        )}

        {activeTab === "transactions" && (
          <>
            <div className="section-title">
              <h2>All transactions</h2>
              <span className="eyebrow">{customerName} · newest first</span>
            </div>
            <AddTransaction customerId={activeCustomer} onPosted={refresh} />
            <TransactionsTable transactions={transactions} />
          </>
        )}

        {activeTab === "budgets" && (
          <>
            <div className="section-title">
              <h2>Budgets</h2>
              <span className="eyebrow">
                spent / limit · dashed tick is the projected month end
              </span>
            </div>
            <div className="budget-grid">
              {budgets.map((b) => (
                <BudgetBar key={b.category} {...b} />
              ))}
            </div>
            {budgets.length === 0 && (
              <div className="ledger-empty box">
                No budgets set for {customerName} yet.
              </div>
            )}

            {suggestions.length > 0 && (
              <>
                <div className="section-title">
                  <h2>Suggested limits</h2>
                  <span className="eyebrow">from this customer's own median month</span>
                </div>
                <BudgetSuggestions
                  customerId={activeCustomer}
                  suggestions={suggestions}
                  onApplied={refresh}
                />
              </>
            )}
          </>
        )}

        {activeTab === "insights" && (
          <>
            <div className="section-title">
              <h2>Findings</h2>
              <span className="eyebrow">
                {insights.length} for {customerName} · worst first
              </span>
            </div>
            <div className="insight-grid">
              {insights.map((insight) => (
                <InsightCard key={insight.id} insight={insight} />
              ))}
            </div>
            {insights.length === 0 && (
              <div className="ledger-empty box">
                Nothing flagged. Either the month is clean, or the backend isn't running.
              </div>
            )}
          </>
        )}

        {activeTab === "advisor" && (
          <div className="advisor-grid">
            <ChatPanel customerId={activeCustomer} expanded />
            <div className="advisor-side">
              <HealthPanel health={health} account={account} />
            </div>
          </div>
        )}

        {activeTab === "bank" && <BankConsole onOpenCustomer={openCustomer} />}
      </div>
    </div>
  );
}
