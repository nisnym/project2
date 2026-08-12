import { useState } from "react";
import { addTransaction } from "../api";

// The live-demo lever. Post a spend here and every derived number — budget
// status, projection, insight, health score, what the advisor says — moves on
// the next render, because none of it is precomputed.
export default function AddTransaction({ customerId, onPosted }) {
  const [merchant, setMerchant] = useState("");
  const [category, setCategory] = useState("dining");
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [posted, setPosted] = useState(null);

  async function submit(event) {
    event.preventDefault();
    if (busy) return;

    const value = Number(amount);
    if (!merchant.trim() || !value) {
      setError("Merchant and a non-zero amount are both needed.");
      return;
    }

    setBusy(true);
    setError(null);
    try {
      // Negative = money out. Typing 850 means you spent ₹850.
      const created = await addTransaction(customerId, {
        merchant: merchant.trim(),
        category: category.trim().toLowerCase(),
        amount: -Math.abs(value),
      });
      setPosted(created);
      setMerchant("");
      setAmount("");
      onPosted?.(created);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="add-txn box" onSubmit={submit}>
      <span className="eyebrow add-txn-label">post a spend</span>

      <input
        className="add-txn-input mono"
        placeholder="merchant"
        value={merchant}
        onChange={(e) => setMerchant(e.target.value)}
      />
      <input
        className="add-txn-input mono add-txn-input--category"
        placeholder="category"
        value={category}
        onChange={(e) => setCategory(e.target.value)}
      />
      <input
        className="add-txn-input mono add-txn-input--amount"
        placeholder="₹ amount"
        inputMode="decimal"
        value={amount}
        onChange={(e) => setAmount(e.target.value)}
      />
      <button className="add-txn-send" type="submit" disabled={busy}>
        {busy ? "posting…" : "post"}
      </button>

      {error && <span className="add-txn-error mono">{error}</span>}
      {posted && !error && (
        <span className="add-txn-ok mono">
          {posted.id} posted — budgets and advice updated
        </span>
      )}
    </form>
  );
}
