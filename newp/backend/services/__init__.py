"""The five domain services plus the gateway.

Each subpackage is an independently deployable FastAPI app:

    customer_service      9001  people, accounts, goals
    transaction_service   9002  the ledger + aggregation
    budget_service        9003  limits, live spend, pace, suggestions
    insight_service       9004  recommendations, each with a reason
    chat_service          9005  the advisor (GitHub Models)
    gateway               8080  the only door the SPA knocks on
"""
