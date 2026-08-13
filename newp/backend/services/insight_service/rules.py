"""The advice engine.

House rules, enforced by every function in this file:

1. **No insight without a reason.** `reason` is plain language a customer
   would accept, and it names the numbers it used.
2. **Every number is traceable.** `dataPoints[].source` points at the record
   it came from — `txn:t1055`, `month:2026-07`, `account:acc-001` — so any
   claim on screen can be walked back to this customer's own ledger.
3. **Never invent a fact.** If the history isn't there, the rule stays quiet
   and `thin_history` says so out loud. Silence beats a confident guess.
4. **Nothing is customer-specific.** No rule knows a name or an id; they all
   read the ledger. Swap the data and the advice changes with it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from statistics import median, pstdev
from typing import Any, Optional

from shared.categories import COMMITTED_CATEGORIES
from shared.dates import days_in_month, elapsed_days, month_label, parse_date, today
from shared.models import DataPoint, HealthComponent, HealthScore, Insight
from shared.money import format_inr, pct, round_money

Row = dict[str, Any]


# ----------------------------------------------------------------------
# context
# ----------------------------------------------------------------------

@dataclass
class Context:
    """Everything the rules are allowed to look at, fetched once per request."""

    customer: Row
    account: Optional[Row]
    month: str
    summary: Row                       # current month, partial
    prior_summaries: dict[str, Row]    # completed months, newest first
    monthly: list[Row]                 # trend line
    budgets: list[Row]
    recurring: Row
    transactions: list[Row]            # current month only
    all_transactions: list[Row] = field(default_factory=list)

    # -- convenience ---------------------------------------------------

    @property
    def customer_id(self) -> str:
        return str(self.customer.get("id", ""))

    @property
    def income(self) -> float:
        return float(self.customer.get("monthlyIncome", 0) or 0)

    @property
    def balance(self) -> float:
        return float((self.account or {}).get("balance", 0) or 0)

    @property
    def day(self) -> int:
        return elapsed_days(self.month)

    @property
    def days_left(self) -> int:
        return max(days_in_month(self.month) - self.day, 0)

    @property
    def prior_months(self) -> list[str]:
        return list(self.prior_summaries.keys())

    def spend_in(self, month: str, category: str) -> float:
        summary = self.prior_summaries.get(month) if month != self.month else self.summary
        if not summary:
            return 0.0
        for entry in summary.get("byCategory", []):
            if entry["category"] == category:
                return float(entry["total"])
        return 0.0

    def typical(self, category: str) -> float:
        """Median spend across completed months — a one-off can't set the norm."""
        values = [self.spend_in(m, category) for m in self.prior_months]
        values = [v for v in values if v > 0]
        return round_money(median(values)) if values else 0.0

    def category_txns(self, category: str) -> list[Row]:
        rows = [t for t in self.transactions if t.get("category") == category and float(t.get("amount", 0)) < 0]
        return sorted(rows, key=lambda t: abs(float(t["amount"])), reverse=True)

    def last_full_month(self) -> Optional[Row]:
        return next(iter(self.prior_summaries.values()), None)


def _dp(label: str, value: str, source: Optional[str] = None) -> DataPoint:
    return DataPoint(label=label, value=value, source=source)


def _txn_dp(txn: Row) -> DataPoint:
    return _dp(
        f"{txn.get('merchant', 'unknown')} on {_day_phrase(str(txn.get('date', '')))}",
        format_inr(abs(float(txn.get("amount", 0))), decimals=True),
        f"txn:{txn.get('id')}",
    )


def _day_phrase(iso: str) -> str:
    date = parse_date(iso)
    if not date:
        return iso
    suffix = {1: "st", 2: "nd", 3: "rd", 21: "st", 22: "nd", 23: "rd", 31: "st"}.get(date.day, "th")
    return f"the {date.day}{suffix}"


def _clamp(value: float, low: float = 0, high: float = 100) -> int:
    return int(max(low, min(high, round(value))))


def _join_months(months: list[str]) -> str:
    """['2026-07', '2026-06'] -> 'July and June'."""
    names = [month_label(m).split(" ")[0] for m in months]
    if not names:
        return "earlier months"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


# ----------------------------------------------------------------------
# rules
# ----------------------------------------------------------------------

def budget_breach(ctx: Context) -> list[Insight]:
    """A limit already broken, with the transactions that broke it."""
    out = []
    for budget in ctx.budgets:
        if budget.get("status") != "over":
            continue
        category = budget["category"]
        overshoot = round_money(budget["spentSoFar"] - budget["monthlyLimit"])
        biggest = ctx.category_txns(category)[:3]
        # House rule: an insight never ships without a reason. The budget
        # service always sends one, but we compose our own rather than trust
        # that — an empty reason on screen is worse than a duplicated sentence.
        reason = budget.get("reason") or (
            f"{format_inr(budget['spentSoFar'])} spent against a "
            f"{format_inr(budget['monthlyLimit'])} {category} limit — "
            f"{format_inr(overshoot)} past it, with {ctx.days_left} days left in "
            f"{month_label(ctx.month)}."
        )

        out.append(
            Insight(
                id=f"budget-breach-{category}",
                customerId=ctx.customer_id,
                type="budget_breach",
                severity="critical",
                category=category,
                headline=f"{category.title()} is {format_inr(overshoot)} over its limit",
                detail=(
                    f"{format_inr(budget['spentSoFar'])} spent of a "
                    f"{format_inr(budget['monthlyLimit'])} limit, {ctx.day} days into "
                    f"{month_label(ctx.month)}."
                ),
                reason=reason,
                recommendedAction=(
                    f"Spending nothing more on {category} for the remaining {ctx.days_left} days "
                    f"still ends the month {format_inr(overshoot)} above plan, so the useful "
                    f"decision now is which other category gives that back — or whether the "
                    f"limit itself was wrong."
                ),
                impact=overshoot,
                confidence="high",
                dataPoints=[
                    _dp("Limit", format_inr(budget["monthlyLimit"]), f"budget:{category}"),
                    _dp("Spent so far", format_inr(budget["spentSoFar"]), f"month:{ctx.month}"),
                    *[_txn_dp(t) for t in biggest],
                ],
            )
        )
    return out


def budget_pace_risk(ctx: Context) -> list[Insight]:
    """Still inside the limit, but not on this trajectory."""
    out = []
    for budget in ctx.budgets:
        if budget.get("status") != "at-risk":
            continue
        category = budget["category"]
        headroom = round_money(budget["remaining"])
        daily = round_money(headroom / ctx.days_left) if ctx.days_left else headroom

        out.append(
            Insight(
                id=f"budget-pace-{category}",
                customerId=ctx.customer_id,
                type="budget_pace_risk",
                severity="warning",
                category=category,
                headline=f"{category.title()} is on course to overshoot",
                detail=(
                    f"Projected {format_inr(budget['projectedSpend'])} against a "
                    f"{format_inr(budget['monthlyLimit'])} limit."
                ),
                reason=budget.get("reason")
                or (
                    f"{format_inr(budget['spentSoFar'])} of the {format_inr(budget['monthlyLimit'])} "
                    f"{category} limit is gone {ctx.day} days in, and the month is heading for about "
                    f"{format_inr(budget['projectedSpend'])}."
                ),
                recommendedAction=(
                    f"{format_inr(headroom)} is left for {ctx.days_left} days — about "
                    f"{format_inr(daily)} a day on {category}. Staying under that keeps the month intact."
                ),
                impact=round_money(budget["projectedSpend"] - budget["monthlyLimit"]),
                confidence="medium",
                dataPoints=[
                    _dp("Spent so far", format_inr(budget["spentSoFar"]), f"month:{ctx.month}"),
                    _dp("Limit", format_inr(budget["monthlyLimit"]), f"budget:{category}"),
                    _dp("Projected month-end", format_inr(budget["projectedSpend"])),
                ],
            )
        )
    return out


def category_spike(ctx: Context) -> list[Insight]:
    """A category running hot against this person's own normal.

    Compares part-month spend against the *median completed month*, and says
    so — comparing 12 days to a full month without flagging it would be a
    dishonest number.
    """
    out = []
    for entry in ctx.summary.get("byCategory", []):
        category = entry["category"]
        if category == "income":
            continue
        current = float(entry["total"])
        normal = ctx.typical(category)
        if normal <= 0 or current <= normal * 1.2 or current - normal < 1000:
            continue

        gap = round_money(current - normal)
        biggest = ctx.category_txns(category)
        one_off = biggest[0] if biggest and abs(float(biggest[0]["amount"])) >= gap * 0.6 else None

        if one_off:
            amount = abs(float(one_off["amount"]))
            reason = (
                f"{category.title()} is at {format_inr(current)} this month against a usual "
                f"{format_inr(normal)}, but almost all of the difference is one purchase — "
                f"{one_off['merchant']} for {format_inr(amount)} on {_day_phrase(str(one_off['date']))}. "
                f"Take that out and the rest of your {category} spending is "
                f"{format_inr(current - amount)}, which is normal for you. This is a one-off, "
                f"not a drift."
            )
            severity = "info"
            action = (
                f"Nothing to correct in the habit. Worth checking the {format_inr(amount)} "
                f"came out of savings rather than this month's income."
            )
        else:
            reason = (
                f"You have spent {format_inr(current)} on {category} in the first {ctx.day} days of "
                f"{month_label(ctx.month)}. Your median for a whole month is {format_inr(normal)}, "
                f"from {_join_months(ctx.prior_months)}. "
                f"That is {pct(gap, normal)}% above normal with {ctx.days_left} days still to go, "
                f"and it is spread across {len(biggest)} separate purchases rather than one large one."
            )
            severity = "warning"
            action = (
                f"If the month ended today you would already be {format_inr(gap)} above your usual "
                f"{category} spend. Bringing the next {ctx.days_left} days back to your normal rate "
                f"is what closes the gap."
            )

        out.append(
            Insight(
                id=f"spike-{category}",
                customerId=ctx.customer_id,
                type="category_spike",
                severity=severity,
                category=category,
                headline=(
                    f"One purchase pushed {category} up this month"
                    if one_off
                    else f"{category.title()} is {pct(gap, normal)}% above your normal"
                ),
                detail=f"{format_inr(current)} so far vs a typical {format_inr(normal)}.",
                reason=reason,
                recommendedAction=action,
                impact=gap,
                confidence="high" if len(ctx.prior_months) >= 2 else "medium",
                dataPoints=[
                    _dp(f"{month_label(ctx.month)} so far", format_inr(current), f"month:{ctx.month}"),
                    *[
                        _dp(f"{month_label(m)}", format_inr(ctx.spend_in(m, category)), f"month:{m}")
                        for m in ctx.prior_months
                    ],
                    *([_txn_dp(one_off)] if one_off else [_txn_dp(t) for t in biggest[:2]]),
                ],
            )
        )
    return out


def subscription_creep(ctx: Context) -> list[Insight]:
    """Standing charges are invisible individually and obvious in total.

    Rent, loan instalments and investments are recurring too, but telling
    someone to "cancel their rent" would be nonsense, so they are held out of
    this insight — and the reason says that out loud rather than quietly
    dropping them.
    """
    found = [r for r in ctx.recurring.get("recurring", []) if r["category"] not in COMMITTED_CATEGORIES]
    monthly_total = round_money(sum(float(r["averageAmount"]) for r in found))
    if not found or monthly_total <= 0:
        return []
    if ctx.income and monthly_total < ctx.income * 0.02 and monthly_total < 1500:
        return []

    top = found[:4]
    listing = ", ".join(f"{r['merchant']} {format_inr(r['averageAmount'])}" for r in top)
    annual = round_money(monthly_total * 12)

    return [
        Insight(
            id="subscription-creep",
            customerId=ctx.customer_id,
            type="subscription_creep",
            severity="info",
            headline=f"{format_inr(monthly_total)} a month leaves on autopilot",
            detail=f"{len(found)} recurring charges — {format_inr(annual)} a year.",
            reason=(
                f"These charged you almost the same amount in each of the last "
                f"{len(ctx.recurring.get('monthsExamined', []))} months, so they are standing "
                f"commitments rather than choices you make each month: {listing}. Together they are "
                f"{format_inr(monthly_total)} a month"
                + (f", which is {pct(monthly_total, ctx.income)}% of your monthly income" if ctx.income else "")
                + f", or {format_inr(annual)} a year. Rent, loan instalments and investments repeat "
                f"too, but they are left out of this figure — they are commitments, not subscriptions."
            ),
            recommendedAction=(
                f"Worth one pass through the list. Dropping just the largest, "
                f"{top[0]['merchant']}, returns {format_inr(top[0]['averageAmount'] * 12)} a year."
            ),
            impact=annual,
            confidence="high",
            dataPoints=[
                _dp(
                    r["merchant"],
                    f"{format_inr(r['averageAmount'])} × {r['occurrences']} months",
                    f"txn:{r['transactionIds'][0]}",
                )
                for r in top
            ],
        )
    ]


def savings_rate(ctx: Context) -> list[Insight]:
    """What actually stayed, in the last month that finished."""
    last = ctx.last_full_month()
    if not last:
        return []
    income = float(last.get("totalIncome", 0) or 0)
    spend = float(last.get("totalSpend", 0) or 0)
    if income <= 0:
        return []

    kept = round_money(income - spend)
    rate = pct(kept, income)
    label = month_label(str(last.get("month", "")))
    points = [
        _dp(f"{label} income", format_inr(income), f"month:{last.get('month')}"),
        _dp(f"{label} spending", format_inr(spend), f"month:{last.get('month')}"),
        _dp("Kept", f"{format_inr(kept)} ({rate}%)"),
    ]

    if rate >= 20:
        return [
            Insight(
                id="savings-rate",
                customerId=ctx.customer_id,
                type="savings_rate",
                severity="positive",
                headline=f"You kept {rate}% of what came in last month",
                detail=f"{format_inr(kept)} of {format_inr(income)} in {label}.",
                reason=(
                    f"In {label} you received {format_inr(income)} and spent {format_inr(spend)}, "
                    f"leaving {format_inr(kept)}. Anything above 20% is a strong month, and you are "
                    f"at {rate}%."
                ),
                recommendedAction=(
                    f"If {format_inr(kept)} a month is repeatable, standing it into your goal is "
                    f"worth {format_inr(kept * 12)} over a year."
                ),
                impact=kept,
                confidence="high",
                dataPoints=points,
            )
        ]

    return [
        Insight(
            id="savings-rate",
            customerId=ctx.customer_id,
            type="savings_rate",
            severity="warning" if rate >= 0 else "critical",
            headline=(
                f"Only {rate}% of last month's income stayed with you"
                if rate >= 0
                else f"You spent {format_inr(-kept)} more than you earned last month"
            ),
            detail=f"{format_inr(kept)} of {format_inr(income)} in {label}.",
            reason=(
                f"{label} brought in {format_inr(income)} and {format_inr(spend)} went out, so "
                f"{format_inr(kept)} stayed — {rate}% of your income. A month that keeps less than "
                f"20% leaves nothing to absorb a surprise."
            ),
            recommendedAction=(
                f"Moving {format_inr(max(income * 0.2 - kept, 0))} a month out of your largest "
                f"variable category would put you at the 20% mark."
            ),
            impact=round_money(max(income * 0.2 - kept, 0)),
            confidence="high",
            dataPoints=points,
        )
    ]


def income_timing(ctx: Context) -> list[Insight]:
    """Committed money leaving before earned money arrives."""
    incomes = sorted(
        [t for t in ctx.transactions if float(t.get("amount", 0)) > 0],
        key=lambda t: str(t.get("date", "")),
    )
    if not incomes:
        return []

    first_income = incomes[0]
    before = [
        t
        for t in ctx.transactions
        if float(t.get("amount", 0)) < 0 and str(t.get("date", "")) < str(first_income.get("date", ""))
    ]
    committed = round_money(sum(abs(float(t["amount"])) for t in before))
    if not before or not ctx.income or committed < ctx.income * 0.3:
        return []

    biggest = sorted(before, key=lambda t: abs(float(t["amount"])), reverse=True)[:3]
    naming = ", ".join(
        f"{t['merchant']} {format_inr(abs(float(t['amount'])))} on {_day_phrase(str(t['date']))}"
        for t in biggest
    )
    income_day = _day_phrase(str(first_income["date"]))

    return [
        Insight(
            id="income-timing",
            customerId=ctx.customer_id,
            type="income_timing",
            severity="warning",
            headline=f"{format_inr(committed)} went out before any money came in",
            detail=f"Fixed costs cleared before your first credit of {month_label(ctx.month)}.",
            reason=(
                f"Your first income this month landed on {income_day} "
                f"({first_income['merchant']}, {format_inr(float(first_income['amount']))}). "
                f"Before that, {format_inr(committed)} had already left the account — {naming}. "
                f"For those days the account was carrying committed costs with nothing behind them "
                f"but your balance."
            ),
            recommendedAction=(
                f"Holding {format_inr(committed)} back as a float — one month of fixed costs sitting "
                f"untouched — removes the gap entirely, whatever date the next payment clears."
            ),
            impact=committed,
            confidence="high",
            dataPoints=[
                _dp("First credit", f"{format_inr(float(first_income['amount']))} on {income_day}", f"txn:{first_income['id']}"),
                *[_txn_dp(t) for t in biggest],
            ],
        )
    ]


def cash_runway(ctx: Context) -> list[Insight]:
    """How long the balance lasts at this person's own burn rate."""
    if not ctx.account:
        return []
    last = ctx.last_full_month()
    monthly_spend = float(last.get("totalSpend", 0)) if last else float(ctx.summary.get("totalSpend", 0))
    if monthly_spend <= 0:
        return []

    daily = round_money(monthly_spend / 30)
    days = int(ctx.balance / daily) if daily else 999
    if days >= 30:
        return []

    return [
        Insight(
            id="cash-runway",
            customerId=ctx.customer_id,
            type="low_balance_risk",
            severity="critical" if days < 15 else "warning",
            headline=f"About {days} days of cover left in the account",
            detail=f"{format_inr(ctx.balance)} at roughly {format_inr(daily)} a day.",
            reason=(
                f"Your balance is {format_inr(ctx.balance)}. Across "
                f"{month_label(str(last.get('month'))) if last else 'this month'} you spent "
                f"{format_inr(monthly_spend)}, which is about {format_inr(daily)} a day. At that "
                f"rate the balance covers roughly {days} days."
            ),
            recommendedAction=(
                f"Keeping {format_inr(daily * 30)} — one month of your own spending — as a floor is "
                f"the smallest buffer that stops a late payment turning into an overdraft."
            ),
            impact=round_money(max(daily * 30 - ctx.balance, 0)),
            confidence="medium",
            dataPoints=[
                _dp("Balance", format_inr(ctx.balance), f"account:{ctx.account.get('id')}"),
                _dp("Daily spend", format_inr(daily)),
                _dp("Cover", f"{days} days"),
            ],
        )
    ]


def goal_progress(ctx: Context) -> list[Insight]:
    """Goals measured against what this person actually saves.

    Goals are funded in order of deadline out of one pot of savings, not
    judged independently against the same rupees. Telling someone that three
    goals are each "on track" when together they need more than they save is
    exactly the kind of advice that reads well and is wrong.
    """
    goals = ctx.customer.get("goals") or []
    if not goals:
        return []

    last = ctx.last_full_month()
    if not last:
        return []
    monthly_saving = round_money(float(last.get("totalIncome", 0)) - float(last.get("totalSpend", 0)))
    unclaimed = monthly_saving  # what's left after nearer-dated goals take theirs
    claimed_by_others = 0.0

    out = []
    for goal in sorted(goals, key=lambda g: str(g.get("targetDate") or "9999")):
        target = float(goal.get("targetAmount", 0))
        saved = float(goal.get("savedAmount", 0))
        gap = round_money(target - saved)
        if gap <= 0:
            continue

        target_date = parse_date(goal.get("targetDate") or "")
        if not target_date:
            continue
        months_left = max(
            (target_date.year - today().year) * 12 + (target_date.month - today().month), 0
        )
        if months_left == 0:
            continue

        needed = round_money(gap / months_left)
        on_track = unclaimed >= needed
        eta_months = int(gap / unclaimed) if unclaimed > 0 else None
        eta = (
            (today().replace(day=1) + dt.timedelta(days=31 * eta_months)).strftime("%B %Y")
            if eta_months
            else None
        )

        out.append(
            Insight(
                id=f"goal-{goal.get('id')}",
                customerId=ctx.customer_id,
                type="goal_progress",
                severity="positive" if on_track else "warning",
                headline=(
                    f"{goal['label']} is on track"
                    if on_track
                    else f"{goal['label']} needs {format_inr(needed)} a month"
                ),
                detail=(
                    f"{format_inr(saved)} of {format_inr(target)} saved, "
                    f"{months_left} months to {goal.get('targetDate')}."
                ),
                reason=(
                    f"{goal['label']} needs {format_inr(gap)} more in {months_left} months, which is "
                    f"{format_inr(needed)} a month. Last month you kept {format_inr(monthly_saving)}"
                    + (
                        f", and {format_inr(claimed_by_others)} of that is already spoken for by goals "
                        f"with earlier dates, leaving {format_inr(unclaimed)}"
                        if claimed_by_others
                        else ""
                    )
                    + (
                        f" — enough, with {format_inr(unclaimed - needed)} to spare."
                        if on_track
                        else (
                            f", which is {format_inr(needed - unclaimed)} short. At that rate you reach "
                            f"the target around {eta} instead."
                            if eta
                            else ", so at the moment this goal is not being funded at all."
                        )
                    )
                ),
                recommendedAction=(
                    f"A standing instruction of {format_inr(needed)} on payday, moved before it can be "
                    f"spent, is what makes {goal.get('targetDate')} realistic."
                ),
                impact=round_money(max(needed - monthly_saving, 0)),
                confidence="medium",
                dataPoints=[
                    _dp("Saved so far", format_inr(saved), f"goal:{goal.get('id')}"),
                    _dp("Still needed", format_inr(gap), f"goal:{goal.get('id')}"),
                    _dp("Kept last month", format_inr(monthly_saving), f"month:{last.get('month')}"),
                    _dp("Left for this goal", format_inr(unclaimed)),
                ],
            )
        )

        claimed_by_others = round_money(claimed_by_others + needed)
        unclaimed = round_money(max(monthly_saving - claimed_by_others, 0))

    return out


def steady_categories(ctx: Context) -> list[Insight]:
    """Credit where it's due — a category running below this person's normal."""
    best: Optional[tuple[str, float, float]] = None
    for entry in ctx.summary.get("byCategory", []):
        category = entry["category"]
        if category == "income":
            continue
        normal = ctx.typical(category)
        current = float(entry["total"])
        if normal < 1500:
            continue
        pace = ctx.day / max(days_in_month(ctx.month), 1)
        projected = current / pace if pace else current
        if projected < normal * 0.75:
            saving = round_money(normal - projected)
            if best is None or saving > best[2]:
                best = (category, projected, saving)

    if not best:
        return []
    category, projected, saving = best
    return [
        Insight(
            id=f"improvement-{category}",
            customerId=ctx.customer_id,
            type="positive_trend",
            severity="positive",
            category=category,
            headline=f"{category.title()} is running below your normal",
            detail=f"Heading for about {format_inr(projected)} against a usual {format_inr(ctx.typical(category))}.",
            reason=(
                f"{ctx.day} days in you have spent {format_inr(ctx.spend_in(ctx.month, category))} on "
                f"{category}. Carried to month end that is about {format_inr(projected)}, against your "
                f"usual {format_inr(ctx.typical(category))} — roughly {format_inr(saving)} less than "
                f"a normal month."
            ),
            recommendedAction=(
                f"If it holds, moving that {format_inr(saving)} across to a goal turns an easy month "
                f"into progress instead of it quietly being absorbed elsewhere."
            ),
            impact=saving,
            confidence="medium",
            dataPoints=[
                _dp(f"{month_label(ctx.month)} so far", format_inr(ctx.spend_in(ctx.month, category)), f"month:{ctx.month}"),
                _dp("Your usual month", format_inr(ctx.typical(category))),
            ],
        )
    ]


def thin_history(ctx: Context) -> list[Insight]:
    """The honest answer when there isn't enough to work with."""
    if len(ctx.all_transactions) >= 8:
        return []
    return [
        Insight(
            id="thin-history",
            customerId=ctx.customer_id,
            type="insufficient_data",
            severity="info",
            headline="Not enough history to advise properly yet",
            detail=f"{len(ctx.all_transactions)} transactions on file.",
            reason=(
                f"We can only see {len(ctx.all_transactions)} transactions for this account, across "
                f"{len(ctx.prior_months) + 1} months. Spotting what is normal for someone takes a "
                f"couple of complete months, so anything we said about trends right now would be a "
                f"guess dressed up as advice."
            ),
            recommendedAction="Balances and budgets still work. Trend-based advice unlocks once a full month has posted.",
            confidence="high",
            dataPoints=[_dp("Transactions on file", str(len(ctx.all_transactions)))],
        )
    ]


ALL_RULES = [
    budget_breach,
    budget_pace_risk,
    category_spike,
    income_timing,
    cash_runway,
    savings_rate,
    subscription_creep,
    goal_progress,
    steady_categories,
    thin_history,
]

_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2, "positive": 3}


def run_all(ctx: Context) -> list[Insight]:
    insights: list[Insight] = []
    for rule in ALL_RULES:
        try:
            insights.extend(rule(ctx))
        except Exception:  # noqa: BLE001
            # One bad rule must not take the advisor down mid-demo. The
            # service logs it; the customer just sees the other insights.
            import logging

            logging.getLogger("insight-service").exception("rule %s failed", rule.__name__)
    return sorted(
        insights,
        key=lambda i: (_SEVERITY_RANK.get(i.severity, 9), -(i.impact or 0)),
    )


# ----------------------------------------------------------------------
# health score
# ----------------------------------------------------------------------

def health_score(ctx: Context) -> HealthScore:
    """Four weighted components, each with the sentence that justifies it."""
    components: list[HealthComponent] = []

    # 1. budget discipline
    if ctx.budgets:
        over = [b for b in ctx.budgets if b["status"] == "over"]
        at_risk = [b for b in ctx.budgets if b["status"] == "at-risk"]
        score = _clamp(100 - len(over) * 30 - len(at_risk) * 15)
        if over:
            reason = (
                f"{len(over)} of {len(ctx.budgets)} budgets are already over "
                f"({', '.join(b['category'] for b in over)})."
            )
        elif at_risk:
            reason = f"{len(at_risk)} budgets are projected to overshoot, none broken yet."
        else:
            reason = f"All {len(ctx.budgets)} budgets are inside their limits."
    else:
        score, reason = 60, "No budgets set, so there is nothing to hold spending against."
    components.append(HealthComponent(label="Budget discipline", score=score, weight=0.35, reason=reason))

    # 2. savings rate
    last = ctx.last_full_month()
    if last and float(last.get("totalIncome", 0)) > 0:
        income = float(last["totalIncome"])
        kept = income - float(last.get("totalSpend", 0))
        rate = pct(kept, income)
        score = _clamp(rate / 25 * 100)
        reason = (
            f"{format_inr(kept)} of {format_inr(income)} stayed in "
            f"{month_label(str(last['month']))} — a {rate}% savings rate."
        )
    else:
        score, reason = 50, "No completed month with income on file yet."
    components.append(HealthComponent(label="Savings rate", score=score, weight=0.30, reason=reason))

    # 3. stability
    spends = [float(m["spend"]) for m in ctx.monthly if float(m.get("spend", 0)) > 0][:4]
    if len(spends) >= 2:
        average = sum(spends) / len(spends)
        variation = pstdev(spends) / average if average else 0
        score = _clamp(100 - variation * 200)
        reason = (
            f"Monthly spending has varied by about {round(variation * 100)}% around "
            f"{format_inr(average)} over the last {len(spends)} months."
        )
    else:
        score, reason = 50, "Not enough months on file to judge stability."
    components.append(HealthComponent(label="Spending stability", score=score, weight=0.15, reason=reason))

    # 4. buffer
    monthly_spend = float(last.get("totalSpend", 0)) if last else float(ctx.summary.get("totalSpend", 0))
    if monthly_spend > 0:
        months_cover = ctx.balance / monthly_spend
        score = _clamp(months_cover / 6 * 100)
        reason = (
            f"{format_inr(ctx.balance)} in the account covers about "
            f"{round(months_cover, 1)} months at {format_inr(monthly_spend)} a month."
        )
    else:
        score, reason = 50, "No spending on file to size a buffer against."
    components.append(HealthComponent(label="Cash buffer", score=score, weight=0.20, reason=reason))

    total = _clamp(sum(c.score * c.weight for c in components))
    band = (
        "strong" if total >= 80 else "steady" if total >= 65 else "stretched" if total >= 45 else "strained"
    )
    weakest = min(components, key=lambda c: c.score)

    return HealthScore(
        customerId=ctx.customer_id,
        score=total,
        band=band,
        summary=(
            f"{total}/100 — {band}. Strongest: "
            f"{max(components, key=lambda c: c.score).label.lower()}. "
            f"Weakest: {weakest.label.lower()} — {weakest.reason}"
        ),
        components=components,
    )
