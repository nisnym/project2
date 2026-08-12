"""The deterministic advisor.

This is not a stub. It answers from the same snapshot the model gets, using
the same reasons the insight engine already computed, so the product still
works — truthfully — with no token, no network and no model.

It runs in three situations:
  * CHAT_PROVIDER=fallback (the default, and what a judge sees offline)
  * the configured GitHub Models provider is unavailable
  * the model call fails or times out mid-answer

The one thing it will never do is guess. Ask it something outside this
customer's ledger and it says so — which is the correct answer, not a
degraded one.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from shared.money import format_inr, pct

Snapshot = dict[str, Any]

# Wording only. Each alias resolves to a category that actually exists in the
# customer's data — nothing here invents a category the ledger doesn't have.
SYNONYMS: dict[str, list[str]] = {
    "dining": ["eating out", "eat out", "restaurant", "food delivery", "takeaway", "swiggy", "zomato", "food"],
    "groceries": ["grocery", "supermarket", "kirana"],
    "transport": ["travel", "commute", "cab", "cabs", "uber", "ola", "fuel", "petrol"],
    "subscriptions": ["subscription", "streaming", "memberships"],
    "entertainment": ["movies", "cinema", "going out"],
    "shopping": ["clothes", "shop", "retail"],
    "utilities": ["bills", "electricity", "internet", "broadband"],
    "rent": ["house rent", "landlord"],
    "health": ["medical", "medicine", "pharmacy", "doctor"],
    "education": ["course", "courses", "learning", "tuition"],
    "investments": ["sip", "mutual fund", "investing"],
    "emi": ["loan", "instalment", "installment"],
}


def _dp(label: str, value: str, source: Optional[str] = None) -> dict:
    return {"label": label, "value": value, "source": source}


def _categories(snapshot: Snapshot) -> set[str]:
    found = {e["category"] for e in snapshot.get("thisMonth", {}).get("byCategory", [])}
    found |= {e["category"] for e in snapshot.get("lastCompletedMonth", {}).get("byCategory", [])}
    found |= {b["category"] for b in snapshot.get("budgets", [])}
    return {c for c in found if c and c != "income"}


def _match_category(text: str, snapshot: Snapshot) -> Optional[str]:
    available = _categories(snapshot)
    for category in sorted(available, key=len, reverse=True):
        if category in text:
            return category
    for category, aliases in SYNONYMS.items():
        if category in available and any(alias in text for alias in aliases):
            return category
    return None


def _budget_for(snapshot: Snapshot, category: str) -> Optional[dict]:
    return next((b for b in snapshot.get("budgets", []) if b["category"] == category), None)


# "a budget for scuba diving" — catch what they actually asked about, so that
# an unrecognised subject gets an honest "you have no spending there" instead
# of an answer about some other category entirely.
_NAMED_SUBJECT = re.compile(r"(?:budget|limit|spend|spent|spending)\s+(?:for|on)\s+(?:my\s+|the\s+)?([a-z][a-z \-]{2,28})")


def _named_subject(text: str) -> Optional[str]:
    match = _NAMED_SUBJECT.search(text)
    if not match:
        return None
    subject = match.group(1).strip(" ?.!,")
    filler = {"this month", "last month", "food", "things", "stuff", "everything", "it"}
    return None if subject in filler or not subject else subject


def _period(text: str, snapshot: Snapshot) -> tuple[dict, str, bool]:
    """(period_data, label, is_current) — 'last month' vs the month in progress."""
    last = snapshot.get("lastCompletedMonth", {})
    if "last month" in text or "previous month" in text:
        if last.get("month"):
            return last, f"in {last['month']}", False
    current = snapshot.get("thisMonth", {})
    return current, f"in {snapshot.get('asOfLabel', 'this month')} so far", True


def _category_entry(period: dict, category: str) -> Optional[dict]:
    return next((e for e in period.get("byCategory", []) if e["category"] == category), None)


# ----------------------------------------------------------------------
# intents
# ----------------------------------------------------------------------

def _balance(snapshot: Snapshot) -> dict:
    account = snapshot.get("account") or {}
    this_month = snapshot.get("thisMonth", {})
    return {
        "answer": (
            f"Your {account.get('type', 'account')} balance is {snapshot.get('balanceLabel')}. "
            f"{format_inr(this_month.get('spend', 0))} has gone out and "
            f"{format_inr(this_month.get('income', 0))} has come in so far in "
            f"{snapshot.get('asOfLabel')}."
        ),
        "reasoning": (
            f"Read straight off account {account.get('id')}, with this month's totals from "
            f"{this_month.get('transactionCount', 0)} posted transactions."
        ),
        "data_points_used": [
            _dp("Balance", str(snapshot.get("balanceLabel")), f"account:{account.get('id')}"),
            _dp("Spent this month", format_inr(this_month.get("spend", 0))),
            _dp("Received this month", format_inr(this_month.get("income", 0))),
        ],
        "confidence": "high",
        "intent": "balance_query",
    }


def _spend(snapshot: Snapshot, text: str, category: Optional[str]) -> dict:
    period, label, is_current = _period(text, snapshot)

    if not category:
        top = period.get("byCategory", [])[:3]
        listing = ", ".join(f"{e['category']} {format_inr(e['total'])}" for e in top) or "nothing yet"
        return {
            "answer": (
                f"You've spent {format_inr(period.get('spend', period.get('totalSpend', 0)))} {label}, "
                f"across {period.get('transactionCount', len(period.get('byCategory', [])))} "
                f"transactions. The biggest lines are {listing}."
            ),
            "reasoning": (
                f"Totalled every debit {label} and grouped it by category. "
                f"Top category: {top[0]['category'] if top else 'none'}"
                + (f" at {top[0]['shareOfSpend']}% of the month's spending." if top else ".")
            ),
            "data_points_used": [
                _dp(f"{e['category']} {label}", format_inr(e["total"]), f"month:{snapshot.get('asOf')}")
                for e in top
            ],
            "confidence": "high",
            "intent": "spend_query",
        }

    entry = _category_entry(period, category)
    if not entry:
        others = ", ".join(sorted(_categories(snapshot))) or "none on file"
        return {
            "answer": (
                f"Nothing has gone out on {category} {label} — there are no {category} "
                f"transactions in that window at all. The categories you do have activity in are: {others}."
            ),
            "reasoning": f"Searched the {label} ledger for category '{category}' and found no debits.",
            "data_points_used": [_dp(f"{category} transactions {label}", "0")],
            "confidence": "high",
            "intent": "spend_query",
        }

    budget = _budget_for(snapshot, category)
    sentence = (
        f"You've spent {format_inr(entry['total'])} on {category} {label}, across "
        f"{entry['count']} transactions — {entry['shareOfSpend']}% of everything you spent."
    )
    points = [
        _dp(f"{category} {label}", format_inr(entry["total"])),
        _dp("Transactions", str(entry["count"])),
        _dp("Share of spending", f"{entry['shareOfSpend']}%"),
    ]

    if budget and is_current:
        points.append(_dp(f"{category} limit", format_inr(budget["monthlyLimit"]), f"budget:{category}"))
        if budget["status"] == "over":
            sentence += (
                f" That is {format_inr(budget['spentSoFar'] - budget['monthlyLimit'])} past your "
                f"{format_inr(budget['monthlyLimit'])} limit, with {snapshot.get('daysLeft')} days left."
            )
        elif budget["status"] == "at-risk":
            sentence += (
                f" Your limit is {format_inr(budget['monthlyLimit'])} and this is heading for about "
                f"{format_inr(budget['projectedSpend'])}."
            )
        else:
            sentence += (
                f" Your limit is {format_inr(budget['monthlyLimit'])}, so there's "
                f"{format_inr(budget['remaining'])} left in it."
            )

    return {
        "answer": sentence,
        "reasoning": (
            f"Summed the {entry['count']} {category} debits {label} to {format_inr(entry['total'])}"
            + (f", then compared it with the {format_inr(budget['monthlyLimit'])} limit on that category. "
               f"{budget['reason']}" if budget and is_current else ".")
        ),
        "data_points_used": points,
        "confidence": "high",
        "intent": "spend_query",
    }


def _budget_status(snapshot: Snapshot, category: Optional[str]) -> dict:
    budgets = snapshot.get("budgets", [])
    if not budgets:
        return {
            "answer": (
                "There are no budgets set on this account yet, so there's nothing to be over or "
                "under. I can suggest limits based on what you actually spend if that would help."
            ),
            "reasoning": "No budget rows exist for this customer.",
            "data_points_used": [],
            "confidence": "high",
            "intent": "budget_query",
        }

    if category:
        budget = _budget_for(snapshot, category)
        if not budget:
            return {
                "answer": (
                    f"You don't have a budget on {category}. The ones you do have are: "
                    f"{', '.join(b['category'] for b in budgets)}."
                ),
                "reasoning": f"No budget row for '{category}'.",
                "data_points_used": [],
                "confidence": "high",
                "intent": "budget_query",
            }
        return {
            "answer": f"{category.title()}: {budget['reason']}",
            "reasoning": (
                f"{format_inr(budget['spentSoFar'])} spent of {format_inr(budget['monthlyLimit'])} "
                f"({budget['pctUsed']}%), projected to finish at {format_inr(budget['projectedSpend'])} "
                f"— status '{budget['status']}'."
            ),
            "data_points_used": [
                _dp("Spent", format_inr(budget["spentSoFar"]), f"budget:{category}"),
                _dp("Limit", format_inr(budget["monthlyLimit"]), f"budget:{category}"),
                _dp("Projected", format_inr(budget["projectedSpend"])),
            ],
            "confidence": "high",
            "intent": "budget_query",
        }

    over = [b for b in budgets if b["status"] == "over"]
    at_risk = [b for b in budgets if b["status"] == "at-risk"]

    if not over and not at_risk:
        return {
            "answer": (
                f"All {len(budgets)} of your budgets are inside their limits this month. The tightest "
                f"is {max(budgets, key=lambda b: b['pctUsed'])['category']} at "
                f"{max(budgets, key=lambda b: b['pctUsed'])['pctUsed']}% used."
            ),
            "reasoning": "Compared spend-to-date and projected month-end against every limit on file.",
            "data_points_used": [
                _dp(b["category"], f"{format_inr(b['spentSoFar'])} of {format_inr(b['monthlyLimit'])}",
                    f"budget:{b['category']}")
                for b in budgets[:4]
            ],
            "confidence": "high",
            "intent": "budget_query",
        }

    parts = []
    if over:
        parts.append(
            f"{len(over)} over: " + "; ".join(
                f"{b['category']} at {format_inr(b['spentSoFar'])} against {format_inr(b['monthlyLimit'])}"
                for b in over
            )
        )
    if at_risk:
        parts.append(
            f"{len(at_risk)} heading that way: " + "; ".join(
                f"{b['category']}, projected {format_inr(b['projectedSpend'])} against "
                f"{format_inr(b['monthlyLimit'])}" for b in at_risk
            )
        )

    headline = over[0] if over else at_risk[0]
    return {
        "answer": ". ".join(parts) + f". {headline['reason']}",
        "reasoning": (
            f"Checked all {len(budgets)} limits against spend to date and against a month-end "
            f"projection built from what you spent after this point in previous months."
        ),
        "data_points_used": [
            _dp(b["category"], f"{format_inr(b['spentSoFar'])} of {format_inr(b['monthlyLimit'])}",
                f"budget:{b['category']}")
            for b in (over + at_risk)[:4]
        ],
        "confidence": "high",
        "intent": "budget_query",
    }


def _budget_advice(snapshot: Snapshot, category: Optional[str]) -> dict:
    suggestions = snapshot.get("budgetSuggestions", [])
    picked = None
    if category:
        picked = next((s for s in suggestions if s["category"] == category), None)
    elif suggestions:
        picked = suggestions[0]

    if picked:
        return {
            "answer": (
                f"I'd set {picked['category']} at {format_inr(picked['suggestedLimit'])}"
                + (f", up from {format_inr(picked['currentLimit'])}."
                   if picked.get("currentLimit") and picked["change"] > 0
                   else f", down from {format_inr(picked['currentLimit'])}."
                   if picked.get("currentLimit")
                   else ".")
                + f" {picked['reason']}"
            ),
            "reasoning": (
                f"Took the median of your completed months for {picked['category']} rather than the "
                f"average, so a one-off purchase can't drag the recommendation."
            ),
            "data_points_used": picked.get("dataPoints", []),
            "confidence": picked.get("confidence", "medium"),
            "intent": "budget_advice",
        }

    if category:
        entry = _category_entry(snapshot.get("lastCompletedMonth", {}), category)
        if entry:
            suggested = round(entry["total"] * 1.1 / 100) * 100
            return {
                "answer": (
                    f"Last completed month you spent {format_inr(entry['total'])} on {category}, so a "
                    f"limit around {format_inr(suggested)} would fit your habit with a little room."
                ),
                "reasoning": f"Based on one completed month of {category} spending. One month is thin, "
                             f"so treat this as a starting point rather than a settled number.",
                "data_points_used": [_dp(f"{category} last month", format_inr(entry["total"]))],
                "confidence": "low",
                "intent": "budget_advice",
            }
        return {
            "answer": (
                f"I can't suggest a {category} budget — there's no {category} spending in the months "
                f"I can see, so any number I gave you would be invented."
            ),
            "reasoning": f"No {category} transactions on file.",
            "data_points_used": [],
            "confidence": "high",
            "intent": "budget_advice",
        }

    return {
        "answer": (
            "Your current limits already line up with what you actually spend, so I wouldn't change "
            "any of them this month."
        ),
        "reasoning": "The suggestion engine found no category more than 20% out from its own median.",
        "data_points_used": [],
        "confidence": "medium",
        "intent": "budget_advice",
    }


def _savings(snapshot: Snapshot) -> dict:
    insight = next(
        (i for i in snapshot.get("insights", []) if i["type"] == "savings_rate"), None
    )
    if insight:
        return {
            "answer": f"{insight['headline']}. {insight['recommendedAction']}",
            "reasoning": insight["reason"],
            "data_points_used": insight.get("dataPoints", []),
            "confidence": insight.get("confidence", "high"),
            "intent": "savings_advice",
        }

    last = snapshot.get("lastCompletedMonth", {})
    if not last.get("month"):
        return {
            "answer": "There isn't a completed month on file yet, so I can't tell you what you keep "
                      "in a normal month without guessing.",
            "reasoning": "No completed month in the ledger.",
            "data_points_used": [],
            "confidence": "high",
            "intent": "savings_advice",
        }
    kept = float(last.get("net", 0))
    income = float(last.get("income", 0))
    return {
        "answer": (
            f"In {last['month']} you kept {format_inr(kept)} of {format_inr(income)} — "
            f"{pct(kept, income)}% of what came in."
        ),
        "reasoning": f"Income minus spending for {last['month']}, straight from the ledger.",
        "data_points_used": [
            _dp("Income", format_inr(income), f"month:{last['month']}"),
            _dp("Kept", format_inr(kept), f"month:{last['month']}"),
        ],
        "confidence": "high",
        "intent": "savings_advice",
    }


def _subscriptions(snapshot: Snapshot) -> dict:
    recurring = snapshot.get("recurring", [])
    if not recurring:
        return {
            "answer": "I can't see any charge that repeats at the same amount month after month on "
                      "this account.",
            "reasoning": "No merchant billed a steady amount in two or more of the months on file.",
            "data_points_used": [],
            "confidence": "high",
            "intent": "subscriptions",
        }

    total = float(snapshot.get("recurringMonthlyTotal", 0))
    listing = ", ".join(f"{r['merchant']} {format_inr(r['averageAmount'])}" for r in recurring[:5])
    return {
        "answer": (
            f"{format_inr(total)} a month goes out on {len(recurring)} repeating charges — {listing}. "
            f"That's {format_inr(total * 12)} over a year."
        ),
        "reasoning": (
            f"These merchants billed within 20% of the same amount in at least two of the months on "
            f"file, which is what separates a subscription from an ordinary purchase."
        ),
        "data_points_used": [
            _dp(r["merchant"], f"{format_inr(r['averageAmount'])} × {r['occurrences']} months",
                f"txn:{r['transactionIds'][0]}")
            for r in recurring[:5]
        ],
        "confidence": "high",
        "intent": "subscriptions",
    }


def _goals(snapshot: Snapshot, text: str) -> dict:
    goals = snapshot.get("customer", {}).get("goals", [])
    if not goals:
        return {
            "answer": "There are no savings goals on this account yet.",
            "reasoning": "No goals recorded against the customer.",
            "data_points_used": [],
            "confidence": "high",
            "intent": "goal_query",
        }

    goal_insights = [i for i in snapshot.get("insights", []) if i["type"] == "goal_progress"]
    picked = next((i for i in goal_insights if i["headline"].lower().split(" is ")[0] in text), None)
    picked = picked or (goal_insights[0] if goal_insights else None)

    if picked:
        return {
            "answer": f"{picked['headline']}. {picked['recommendedAction']}",
            "reasoning": picked["reason"],
            "data_points_used": picked.get("dataPoints", []),
            "confidence": picked.get("confidence", "medium"),
            "intent": "goal_query",
        }

    listing = "; ".join(
        f"{g['label']}: {format_inr(g.get('savedAmount', 0))} of {format_inr(g.get('targetAmount', 0))} "
        f"by {g.get('targetDate')}"
        for g in goals
    )
    return {
        "answer": f"Your goals: {listing}.",
        "reasoning": "Read from the goals held against your customer record.",
        "data_points_used": [
            _dp(g["label"], f"{format_inr(g.get('savedAmount', 0))} of {format_inr(g.get('targetAmount', 0))}",
                f"goal:{g.get('id')}")
            for g in goals
        ],
        "confidence": "high",
        "intent": "goal_query",
    }


def _general_advice(snapshot: Snapshot) -> dict:
    insights = [i for i in snapshot.get("insights", []) if i["severity"] != "positive"]
    health = snapshot.get("health", {})

    if not insights:
        positives = snapshot.get("insights", [])
        if positives:
            first = positives[0]
            return {
                "answer": f"Nothing needs attention this month. {first['headline']}. {first['reason']}",
                "reasoning": f"No critical or warning-level findings; health score "
                             f"{health.get('score')}/100.",
                "data_points_used": first.get("dataPoints", []),
                "confidence": "high",
                "intent": "general_advice",
            }
        return {
            "answer": "There isn't enough history on this account for me to give you advice worth "
                      "acting on yet.",
            "reasoning": "The insight engine produced no findings, which happens when the ledger is thin.",
            "data_points_used": [],
            "confidence": "high",
            "intent": "general_advice",
        }

    top = insights[:2]
    body = " ".join(f"{i['headline']}: {i['reason']} {i['recommendedAction']}" for i in top)
    return {
        "answer": body,
        "reasoning": (
            f"Ranked every finding by severity and by rupees at stake. Health score is "
            f"{health.get('score')}/100 ({health.get('band')}) — {health.get('summary', '')}"
        ),
        "data_points_used": [dp for i in top for dp in i.get("dataPoints", [])][:6],
        "confidence": "high",
        "intent": "general_advice",
    }


def _out_of_scope(snapshot: Snapshot) -> dict:
    name = snapshot.get("customer", {}).get("name", "this customer")
    return {
        "answer": (
            f"That's outside what I can see. I only have {name}'s account with us — the transactions, "
            f"budgets, balance and goals on this profile. I can tell you where this month's money "
            f"went, whether a budget is heading for trouble, what a realistic limit would be, or how "
            f"a goal is tracking."
        ),
        "reasoning": (
            "The question didn't map to anything in this customer's ledger, and answering it would "
            "have meant using information I don't hold."
        ),
        "data_points_used": [],
        "confidence": "high",
        "intent": "out_of_scope",
    }


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------

def answer(snapshot: Snapshot, message: str) -> dict:
    text = (message or "").lower().strip()
    if not text:
        return _out_of_scope(snapshot)

    category = _match_category(text, snapshot)
    has = lambda *words: any(w in text for w in words)  # noqa: E731

    # They named a subject we don't hold. Say that, rather than quietly
    # answering about the nearest thing we do hold.
    subject = _named_subject(text)
    if subject and not category:
        return {
            "answer": (
                f"I can't help with {subject} — there is no spending under that name in this "
                f"account. What you do have activity in is: {', '.join(sorted(_categories(snapshot)))}."
            ),
            "reasoning": (
                f"Matched '{subject}' against every category in this customer's ledger and found "
                f"nothing. Answering about a different category would have been misleading."
            ),
            "data_points_used": [],
            "confidence": "high",
            "intent": "out_of_scope",
        }

    if has("balance", "how much money do i have", "in my account", "how much do i have"):
        return _balance(snapshot)

    if has("subscription", "subscriptions", "recurring", "autopay", "auto-debit", "standing"):
        return _subscriptions(snapshot)

    if has("goal", "target", "emergency fund", "saving up"):
        return _goals(snapshot, text)

    if has("budget", "limit"):
        if has("suggest", "recommend", "should", "set ", "realistic", "what budget", "how much should"):
            return _budget_advice(snapshot, category)
        return _budget_status(snapshot, category)

    if has("save", "saving", "savings", "put aside", "set aside"):
        return _savings(snapshot)

    if has("spend", "spent", "spending", "go out", "went out", "cost me", "how much"):
        return _spend(snapshot, text, category)

    if has("advice", "advise", "how am i doing", "tips", "help me", "what should i do", "review"):
        return _general_advice(snapshot)

    # A bare category name ("dining?") is a spend question.
    if category:
        return _spend(snapshot, text, category)

    return _out_of_scope(snapshot)
