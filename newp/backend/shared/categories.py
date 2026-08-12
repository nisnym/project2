"""Category policy — the few opinions the product holds about spending.

Everything else in this system is derived from the customer's data. These two
sets are the exception: they are judgements we have made on their behalf, so
they live in one file, named, rather than being scattered through the rules as
magic strings. Both are also documented in the README.

Categories in the data are free text. Anything not listed here is treated as
ordinary discretionary spending, which is the safe default.
"""

# Obligations, not choices. You cannot "cancel" your rent or your loan, so
# these are never presented as things to trim.
COMMITTED_CATEGORIES = {
    "rent",
    "emi",
    "loan",
    "insurance",
    "tax",
    "investments",  # money moving to savings is not money lost
}

# Categories we do not propose a monthly limit for. The committed ones because
# the amount isn't yours to choose, income because it isn't spending, and
# health because putting a cap on medical spending is not advice a bank should
# be giving. They are still tracked, still shown, still counted — we simply
# don't recommend a ceiling.
NON_BUDGETABLE_CATEGORIES = COMMITTED_CATEGORIES | {"income", "health"}
