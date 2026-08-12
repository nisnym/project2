"""Prompt construction.

Split out from transport and from the fallback on purpose: this is the file
to iterate on when tuning the advisor's voice, and nothing else has to change
when you do.

The model never sees the database. It sees the same snapshot the UI renders —
`GET :9004/insights/{id}/snapshot` — so any figure it quotes is a figure a
judge can pull up in a browser tab.
"""

from __future__ import annotations

import json
from typing import Any

from shared.money import format_inr

SYSTEM_PROMPT = """You are the in-app financial advisor for an Indian retail bank.

You are talking to the account holder about their own money. Everything you \
know is in the FACTS block: it is this customer's real ledger for the current \
and previous months. All amounts are Indian rupees.

Rules, in order of importance:

1. Never state a number that is not in FACTS, and never estimate one. If the \
   answer needs a figure you do not have, say plainly that you cannot see it \
   and name what you would need.
2. Every recommendation carries its reason in the same breath, in language a \
   non-technical customer would accept, and the reason must point at this \
   customer's own data ("your dining is at Rs 11,900 against your Rs 8,000 \
   limit"), never at general advice ("dining out is expensive").
3. Distinguish a habit from a one-off. A single large purchase is not a trend, \
   and saying so is more useful than an alarm.
4. Be short. Two or three sentences unless asked for more. No bullet lists \
   unless the customer asks for a plan.
5. No moralising, no praise-sandwiches, no "as an AI". You are a bank, not a \
   life coach.
6. If asked something outside this customer's finances — the weather, stock \
   tips, another person's account, anything requiring information you do not \
   have — say you cannot help with that and state what you can do instead. \
   Guessing is worse than declining.

Reply with a single JSON object and nothing else:

{
  "answer":   "what you say to the customer",
  "reasoning": "the specific figures from FACTS that led there",
  "data_points_used": [{"label": "...", "value": "...", "source": "txn:t1055"}],
  "confidence": "high | medium | low",
  "intent": "spend_query | budget_query | budget_advice | savings_advice | goal_query | balance_query | subscriptions | general_advice | out_of_scope"
}

Use "low" confidence whenever the FACTS only partly cover the question, and \
say so in the answer too."""


def render_facts(snapshot: dict[str, Any]) -> str:
    """Flatten the snapshot into the compact, quotable FACTS block."""
    customer = snapshot.get("customer", {})
    this_month = snapshot.get("thisMonth", {})
    last_month = snapshot.get("lastCompletedMonth", {})

    lines: list[str] = []
    add = lines.append

    add(f"AS OF: {snapshot.get('asOfLabel')} — day {snapshot.get('daysElapsed')}, "
        f"{snapshot.get('daysLeft')} days left in the month.")
    add(
        f"CUSTOMER: {customer.get('name')} · {customer.get('segment')} · "
        f"{customer.get('city')} · income {format_inr(customer.get('monthlyIncome', 0))}/month "
        f"({customer.get('incomeStability')}) · balance {snapshot.get('balanceLabel')}"
    )

    add("")
    add(f"THIS MONTH SO FAR: spent {format_inr(this_month.get('spend', 0))}, "
        f"received {format_inr(this_month.get('income', 0))}, "
        f"{this_month.get('transactionCount', 0)} transactions.")
    for entry in this_month.get("byCategory", []):
        add(f"  - {entry['category']}: {format_inr(entry['total'])} "
            f"across {entry['count']} transactions ({entry['shareOfSpend']}% of spend)")

    if last_month.get("month"):
        add("")
        add(f"LAST COMPLETED MONTH ({last_month['month']}): spent "
            f"{format_inr(last_month.get('spend', 0))}, received "
            f"{format_inr(last_month.get('income', 0))}, kept "
            f"{format_inr(last_month.get('net', 0))}.")
        for entry in last_month.get("byCategory", [])[:8]:
            add(f"  - {entry['category']}: {format_inr(entry['total'])}")

    budgets = snapshot.get("budgets", [])
    if budgets:
        add("")
        add("BUDGETS (limit / spent / projected / status):")
        for b in budgets:
            add(f"  - {b['category']}: {format_inr(b['monthlyLimit'])} / "
                f"{format_inr(b['spentSoFar'])} / {format_inr(b['projectedSpend'])} / {b['status']}")
            if b.get("reason"):
                add(f"      why: {b['reason']}")

    recurring = snapshot.get("recurring", [])
    if recurring:
        add("")
        add(f"RECURRING CHARGES (total {format_inr(snapshot.get('recurringMonthlyTotal', 0))}/month):")
        for r in recurring[:8]:
            add(f"  - {r['merchant']}: {format_inr(r['averageAmount'])} × {r['occurrences']} months")

    goals = customer.get("goals", [])
    if goals:
        add("")
        add("GOALS:")
        for g in goals:
            add(f"  - {g['label']}: {format_inr(g.get('savedAmount', 0))} of "
                f"{format_inr(g.get('targetAmount', 0))} by {g.get('targetDate')}")

    health = snapshot.get("health", {})
    if health:
        add("")
        add(f"HEALTH SCORE: {health.get('score')}/100 ({health.get('band')}). {health.get('summary')}")

    insights = snapshot.get("insights", [])
    if insights:
        add("")
        add("WHAT WE HAVE ALREADY FLAGGED (reuse these reasons, don't contradict them):")
        for i in insights[:6]:
            add(f"  - [{i['severity']}] {i['headline']} — {i['reason']}")

    recent = snapshot.get("recentTransactions", [])
    if recent:
        add("")
        add("RECENT TRANSACTIONS (id · date · amount · category · merchant):")
        for t in recent[:12]:
            add(f"  - {t['id']} · {t['date']} · {format_inr(t['amount'], decimals=True, signed=True)} "
                f"· {t['category']} · {t['merchant']}")

    return "\n".join(lines)


def build_messages(snapshot: dict[str, Any], message: str, history: list[dict]) -> list[dict]:
    """OpenAI-shaped messages. GitHub Models speaks the same wire format."""
    facts = render_facts(snapshot)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"FACTS\n=====\n{facts}"},
    ]
    for turn in history[-6:]:  # a short memory is enough, and keeps tokens down
        role = turn.get("role")
        if role in {"user", "assistant"} and turn.get("content"):
            messages.append({"role": role, "content": str(turn["content"])})
    messages.append({"role": "user", "content": message})
    return messages


def parse_model_reply(raw: str) -> dict:
    """Read the model's JSON, and survive it not being JSON.

    Models occasionally wrap JSON in prose or a code fence. Rather than fail
    the request, take what we can and mark the confidence down.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict) and parsed.get("answer"):
                return parsed
        except json.JSONDecodeError:
            pass

    return {
        "answer": text or "I couldn't put an answer together for that one.",
        "reasoning": "The model replied in prose rather than the agreed JSON shape, "
                     "so this answer is shown as written and was not checked field by field.",
        "data_points_used": [],
        "confidence": "low",
        "intent": "unknown",
    }
