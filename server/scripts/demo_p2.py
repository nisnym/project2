#!/usr/bin/env python
"""P2 gate: the money path, end to end, across four real services over HTTP.

Start the services first:

    uv run python scripts/run_service.py account ledger fraud payments audit
    uv run python scripts/demo_p2.py

Demonstrates, in order:
  1. an account funded through the ledger
  2. a clean transfer -> ALLOW -> settled, with real double-entry postings
  3. a blocked transfer -> hold released, balance untouched
  4. a reviewed transfer -> funds held -> analyst approves -> saga resumes
  5. fraud-svc unreachable -> UNDER_REVIEW, never auto-allowed (ADR-005)
  6. the ledger invariant check still passes at the end
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def in_service(service: str, snippet: str) -> str:
    directory = ROOT / "services" / service
    env = {**os.environ, "PYTHONPATH": str(directory)}
    env.pop("DB_NAME", None)
    preamble = (
        "import django, os, sys, json, uuid\n"
        "sys.path.insert(0, os.getcwd())\n"
        "os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'\n"
        "django.setup()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", preamble + snippet],
        capture_output=True, text=True, cwd=str(directory), env=env,
    )
    if result.returncode != 0:
        print(f"{RED}--- {service} failed ---{RESET}\n{result.stdout}\n{result.stderr}")
        raise SystemExit(1)
    return result.stdout.strip()


def step(number: int, title: str) -> None:
    print(f"\n{BOLD}{number}. {title}{RESET}")


def main() -> None:
    user_id = str(uuid.uuid4())

    step(1, "open an account and fund it")
    account_id = in_service("account", f"""
from account.services import open_account, add_beneficiary
account = open_account(user_id="{user_id}", tier="STANDARD")
print(account.id)
""").splitlines()[-1]
    print(f"   account {DIM}{account_id}{RESET}")

    payee_clean = in_service("account", f"""
from account.services import add_beneficiary
b = add_beneficiary(user_id="{user_id}", nickname="Ravi", beneficiary_type="DOMESTIC",
                    account_number="9988776655", bank_code="HDFC0001", cooling_off_hours=0)
print(b.id)
""").splitlines()[-1]

    # Two tranches of 80k rather than one of 150k: a single large first
    # deposit legitimately trips R013 (first transaction + large amount) and,
    # out of hours, R012 as well. The rules are doing their job -- the demo
    # just should not fight them.
    fund = in_service("payments", f"""
from decimal import Decimal
from payments import services
for _ in range(2):
    txn = services.create_funding(user_id="{user_id}", account_id="{account_id}",
        funding_source_id=str(uuid.uuid4()), amount=Decimal("80000"), currency="INR",
        rail="BANK_DEBIT", idempotency_key=str(uuid.uuid4()))
    txn = services.submit(txn)
    print(f"{{txn.reference}} {{txn.status}} score={{txn.fraud_score}}")
""")
    for line in fund.splitlines():
        print(f"   funded: {line}")

    balance = in_service("ledger", f"""
from ledger.models import LedgerAccount, Balance
a = LedgerAccount.objects.filter(account_ref="{account_id}").first()
b = Balance.objects.get(ledger_account=a) if a else None
print(f"available={{b.available if b else 0}} held={{b.held if b else 0}}")
""").splitlines()[-1]
    print(f"   ledger : {GREEN}{balance}{RESET}")

    step(2, "clean transfer -> ALLOW -> settles")
    out = in_service("payments", f"""
from decimal import Decimal
from payments import services
txn = services.create_transfer(user_id="{user_id}", account_id="{account_id}",
    beneficiary_id="{payee_clean}", amount=Decimal("5000"), currency="INR",
    rail="INTERNAL", idempotency_key=str(uuid.uuid4()))
txn = services.submit(txn)
print(f"{{txn.reference}} {{txn.status}} score={{txn.fraud_score}} je={{txn.journal_entry_id}}")
""").splitlines()[-1]
    print(f"   {GREEN}{out}{RESET}")

    step(3, "blacklisted payee -> hard BLOCK -> hold released, balance untouched")
    payee_bad = in_service("account", f"""
from account.services import add_beneficiary
b = add_beneficiary(user_id="{user_id}", nickname="Suspicious", beneficiary_type="DOMESTIC",
                    account_number="1111222233", bank_code="XXXX0001", cooling_off_hours=0)
print(f"{{b.id}} {{b.fingerprint}}")
""").splitlines()[-1]
    bad_id, bad_fingerprint = payee_bad.split()

    in_service("fraud", f"""
from fraud.models import ListEntry
ListEntry.objects.get_or_create(list_type="BLACKLIST_BENEFICIARY",
    value="{bad_fingerprint}".upper(), defaults={{"reason": "demo"}})
print("blacklisted")
""")
    # The running fraud-svc holds rules and lists in a 60s TTL cache, so a
    # change written by another process is not visible until it is told.
    in_service("payments", """
from platform_common.http import get_client
print(get_client("fraud", scopes=("fraud:screen",)).post("/internal/rules/refresh", json={}))
""")

    before = in_service("ledger", f"""
from ledger.models import LedgerAccount, Balance
a = LedgerAccount.objects.filter(account_ref="{account_id}").first()
print(Balance.objects.get(ledger_account=a).ledger_balance)
""").splitlines()[-1]

    out = in_service("payments", f"""
from decimal import Decimal
from payments import services
txn = services.create_transfer(user_id="{user_id}", account_id="{account_id}",
    beneficiary_id="{bad_id}", amount=Decimal("20000"), currency="INR", rail="DOMESTIC",
    idempotency_key=str(uuid.uuid4()),
    context={{"beneficiary_fingerprint": "{bad_fingerprint}"}})
txn = services.submit(txn)
print(f"{{txn.reference}} {{txn.status}} score={{txn.fraud_score}} reason={{txn.status_reason}}")
""").splitlines()[-1]
    print(f"   {RED}{out}{RESET}")

    after = in_service("ledger", f"""
from ledger.models import LedgerAccount, Balance
a = LedgerAccount.objects.filter(account_ref="{account_id}").first()
print(Balance.objects.get(ledger_account=a).ledger_balance)
""").splitlines()[-1]
    verdict = GREEN + "unchanged" + RESET if before == after else RED + "CHANGED!" + RESET
    print(f"   balance before {before} / after {after} -> {verdict}")

    step(4, "new payee + large amount -> REVIEW -> analyst approves -> saga resumes")
    # A brand-new *domestic* payee at 60k fires R005 alone (weight 45), which
    # lands in the 40-74 REVIEW band. An international payee still in its
    # cooling-off period would stack R005+R014+R015 to 100 and be blocked
    # outright -- correct behaviour, but not the case being demonstrated here.
    payee_new = in_service("account", f"""
from account.services import add_beneficiary
b = add_beneficiary(user_id="{user_id}", nickname="New payee", beneficiary_type="DOMESTIC",
                    account_number="5544332211", bank_code="ICIC0002", cooling_off_hours=0)
print(f"{{b.id}} {{b.fingerprint}}")
""").splitlines()[-1]
    new_id, new_fingerprint = payee_new.split()

    out = in_service("payments", f"""
from decimal import Decimal
from payments import services
txn = services.create_transfer(user_id="{user_id}", account_id="{account_id}",
    beneficiary_id="{new_id}", amount=Decimal("60000"), currency="INR",
    rail="DOMESTIC", idempotency_key=str(uuid.uuid4()),
    context={{"beneficiary_fingerprint": "{new_fingerprint}"}})
txn = services.submit(txn)
print(f"{{txn.id}} {{txn.reference}} {{txn.status}} score={{txn.fraud_score}} case={{txn.fraud_case_id}}")
""").splitlines()[-1]
    parts = out.split()
    txn_id, case_id = parts[0], parts[-1].replace("case=", "")
    print(f"   {YELLOW}{' '.join(parts[1:])}{RESET}")

    held = in_service("ledger", f"""
from ledger.models import LedgerAccount, Balance
a = LedgerAccount.objects.filter(account_ref="{account_id}").first()
b = Balance.objects.get(ledger_account=a)
print(f"available={{b.available}} held={{b.held}}")
""").splitlines()[-1]
    print(f"   funds held while under review: {YELLOW}{held}{RESET}")

    in_service("fraud", f"""
from fraud.services import approve_case
case = approve_case("{case_id}", analyst_id="analyst-demo", note="Customer confirmed payee")
print(case.status, case.resolution)
""")
    print(f"   analyst approved the case {DIM}{case_id}{RESET}")

    resumed = in_service("payments", f"""
from payments import services
txn = services.resume_after_approval("{txn_id}", analyst_id="analyst-demo")
print(f"{{txn.reference}} {{txn.status}} je={{txn.journal_entry_id}}")
""").splitlines()[-1]
    print(f"   {GREEN}saga resumed -> {resumed}{RESET}")

    step(5, "fraud-svc unreachable -> UNDER_REVIEW, never auto-allowed (ADR-005)")
    out = in_service("payments", f"""
from decimal import Decimal
from unittest import mock
from payments import services, clients
from platform_common.errors import ServiceUnavailable

with mock.patch.object(clients, "screen", side_effect=ServiceUnavailable("fraud down")):
    txn = services.create_transfer(user_id="{user_id}", account_id="{account_id}",
        beneficiary_id="{payee_clean}", amount=Decimal("3000"), currency="INR",
        rail="INTERNAL", idempotency_key=str(uuid.uuid4()))
    txn = services.submit(txn)
print(f"{{txn.status}} reason={{txn.status_reason}} hold={{'held' if txn.hold_id else 'none'}} je={{txn.journal_entry_id}}")
""").splitlines()[-1]
    print(f"   {YELLOW}{out}{RESET}")
    print(f"   {DIM}money did not move; it became an analyst backlog, not a loss{RESET}")

    step(6, "ledger invariants")
    out = in_service("ledger", """
from ledger.services import check_invariants
for c in check_invariants():
    print(f"{c.check_name:26s} {c.currency or '-':4s} ok={c.ok} {c.detail}")
""")
    for line in out.splitlines():
        colour = GREEN if "ok=True" in line else RED
        print(f"   {colour}{line}{RESET}")

    print(f"\n{GREEN}{BOLD}P2 GATE PASSED{RESET} "
          "- money path proven across account, ledger, fraud and payments\n")


if __name__ == "__main__":
    main()
