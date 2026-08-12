"""The deterministic advisor.

What is being tested here is mostly restraint: that it answers from the
snapshot when it can, and declines clearly when it can't.
"""

from services.chat_service import fallback

SNAPSHOT = {
    "asOf": "2026-08",
    "asOfLabel": "August 2026",
    "daysElapsed": 12,
    "daysLeft": 19,
    "balanceLabel": "₹2,14,561",
    "account": {"id": "acc-001", "type": "savings", "balance": 214560.75},
    "customer": {
        "id": "cust-001",
        "name": "Ananya Iyer",
        "monthlyIncome": 145000,
        "goals": [{"id": "g1", "label": "Japan trip", "targetAmount": 250000, "savedAmount": 60000}],
    },
    "thisMonth": {
        "spend": 106000,
        "income": 145000,
        "transactionCount": 19,
        "byCategory": [
            {"category": "dining", "total": 11900, "count": 6, "shareOfSpend": 11.2},
            {"category": "groceries", "total": 5300, "count": 2, "shareOfSpend": 5.0},
        ],
    },
    "lastCompletedMonth": {"month": "2026-07", "spend": 78765, "income": 145000, "net": 66235, "byCategory": []},
    "budgets": [
        {
            "category": "dining", "monthlyLimit": 8000, "spentSoFar": 11900, "remaining": -3900,
            "pctUsed": 148.8, "status": "over", "projectedSpend": 13865,
            "reason": "₹11,900 spent against an ₹8,000 limit.",
        }
    ],
    "recurring": [
        {"merchant": "Netflix", "averageAmount": 799, "occurrences": 3, "transactionIds": ["t1010"]}
    ],
    "recurringMonthlyTotal": 799,
    "health": {"score": 63, "band": "stretched", "summary": "Weakest: budget discipline."},
    "insights": [
        {
            "id": "i1", "type": "budget_breach", "severity": "critical",
            "headline": "Dining is ₹3,900 over its limit", "reason": "Because ₹11,900 vs ₹8,000.",
            "recommendedAction": "Decide which category gives it back.", "dataPoints": [],
            "confidence": "high",
        }
    ],
    "budgetSuggestions": [
        {
            "category": "dining", "currentLimit": 8000, "suggestedLimit": 9900, "change": 1900,
            "reason": "A limit you always break stops being useful.", "confidence": "high", "dataPoints": [],
        }
    ],
    "recentTransactions": [],
}


def ask(question):
    return fallback.answer(SNAPSHOT, question)


class TestItAnswersWhatItKnows:
    def test_category_spend_with_the_budget_context(self):
        reply = ask("how much did I spend on dining?")
        assert reply["intent"] == "spend_query"
        assert "₹11,900" in reply["answer"]
        assert "₹8,000" in reply["answer"]  # the limit, unprompted

    def test_an_alias_resolves_to_a_real_category(self):
        assert "₹11,900" in ask("how much on eating out?")["answer"]

    def test_balance(self):
        reply = ask("what's my balance?")
        assert reply["intent"] == "balance_query"
        assert "₹2,14,561" in reply["answer"]

    def test_budget_status_names_what_is_over(self):
        reply = ask("am I over budget anywhere?")
        assert reply["intent"] == "budget_query"
        assert "dining" in reply["answer"]

    def test_budget_advice_uses_the_suggestion_engine(self):
        reply = ask("what budget should I set for dining?")
        assert reply["intent"] == "budget_advice"
        assert "₹9,900" in reply["answer"]

    def test_general_advice_leads_with_the_worst_finding(self):
        reply = ask("any advice for me?")
        assert reply["intent"] == "general_advice"
        assert "Dining is ₹3,900 over its limit" in reply["answer"]

    def test_subscriptions(self):
        reply = ask("what subscriptions am I paying for?")
        assert "Netflix" in reply["answer"]
        assert reply["data_points_used"][0]["source"] == "txn:t1010"


class TestItDeclinesWhatItDoesNot:
    def test_the_weather_is_not_its_business(self):
        reply = ask("what's the weather in Goa tomorrow?")
        assert reply["intent"] == "out_of_scope"
        assert "Ananya" in reply["answer"]

    def test_it_will_not_answer_about_a_category_that_does_not_exist(self):
        reply = ask("how much did I spend on scuba diving?")
        assert reply["intent"] == "out_of_scope"
        assert "scuba diving" in reply["answer"]

    def test_a_known_category_with_no_spending_says_zero_not_nothing(self):
        reply = ask("how much did I spend on groceries?")
        assert "₹5,300" in reply["answer"]

    def test_empty_input_is_handled(self):
        assert ask("")["intent"] == "out_of_scope"


class TestEveryAnswerCarriesItsReasoning:
    def test_all_intents_return_the_full_shape(self):
        questions = [
            "how much did I spend on dining?",
            "am I over budget?",
            "what's my balance?",
            "any advice?",
            "how are my goals?",
            "what subscriptions do I have?",
            "how much am I saving?",
            "tell me a joke",
        ]
        for question in questions:
            reply = ask(question)
            assert reply["answer"], question
            assert reply["reasoning"], question
            assert reply["confidence"] in {"high", "medium", "low"}, question
            assert isinstance(reply["data_points_used"], list), question
