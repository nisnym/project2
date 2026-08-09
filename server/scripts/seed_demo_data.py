#!/usr/bin/env python
"""Put the four consoles into a state worth looking at.

Drives the *public* API as a real customer would, so everything it creates is
reachable in the UI and nothing is written behind the application's back.

    uv run python scripts/run_service.py --all
    (cd services/identity && uv run python manage.py seed_demo_users)
    uv run python scripts/seed_demo_data.py

Produces, deliberately:
  * a settled top-up and a clean transfer          -> customer activity
  * a transfer held for REVIEW                     -> analyst queue
  * a transfer BLOCKED outright                    -> customer sees a stop
  * an active standing order                       -> recurring transfers

The two fraud outcomes are produced by *real rules firing on real signals*, not
by forcing a status: a brand-new domestic payee at a large amount lands in the
review band, and a brand-new international payee inside its cooling-off window
stacks enough weight to be blocked.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

ROUTES = {
    "/api/auth": 8001, "/api/onboarding": 8002, "/api/accounts": 8004,
    "/api/beneficiaries": 8004, "/api/transactions": 8005, "/api/transfers": 8005,
    "/api/funding-sources": 8005, "/api/funding": 8005, "/api/schedules": 8005,
    "/api/fraud": 8007,
}

EMAIL = "asha@indbank.test"
PASSWORD = "demo-password-2026"

# A stable device, as a returning customer's browser would be.
DEVICE = "web:seed-demo-device"

# R002 and R003 score on a 5-minute window. Two seeder runs inside that window
# genuinely look like a burst of activity, and every transfer then scores 100 --
# the rules working correctly on the data they were given, which is confusing to
# watch. So the seeder waits the window out rather than producing a state that
# misrepresents its own rules.
VELOCITY_WINDOW_S = 305


def call(path, *, method="GET", token=None, body=None, key=None):
    for prefix in sorted(ROUTES, key=len, reverse=True):
        if path.startswith(prefix):
            url = f"http://127.0.0.1:{ROUTES[prefix]}{path}"
            break
    else:
        raise SystemExit(f"no service owns {path!r}")

    request = urllib.request.Request(url, method=method)
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if key:
        request.add_header("Idempotency-Key", key)
    if method != "GET":
        # Parity with the SPA, which sends both on every mutation. Without them
        # the device reads as unrecognised and the country as changed, so R008
        # (impossible travel) and R009 (new device) fire on transfers that have
        # nothing wrong with them -- noise that buries the rules being shown.
        request.add_header("X-Device-Fingerprint", DEVICE)
        request.add_header("X-Ip-Country", "IN")
    if body is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(body).encode()

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"raw": raw.decode()[:300]}
    except urllib.error.URLError as error:
        raise SystemExit(
            f"{RED}{url} is not reachable ({error.reason}).{RESET}\n"
            f"Start the estate: uv run python scripts/run_service.py --all"
        )


def note(message):
    print(f"  {DIM}{message}{RESET}")


def wait_out_velocity_window(token) -> None:
    """Sleep until this account's 5-minute velocity window is clear.

    Without this a second run inside five minutes stacks R002 and R003 onto
    every transfer, so even a 2,500 rupee payment scores 100 and blocks. That is
    the ruleset behaving correctly, but it makes the seeded state useless as a
    demonstration of the *other* rules.
    """
    import time

    status, recent = call("/api/transactions?limit=1", token=token)
    if status != 200 or not recent.get("results"):
        return

    last = datetime.fromisoformat(recent["results"][0]["created_at"])
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age >= VELOCITY_WINDOW_S:
        return

    remaining = int(VELOCITY_WINDOW_S - age)
    print(f"  {YELLOW}this account transacted {int(age)}s ago{RESET}")
    note(f"waiting {remaining}s for the velocity window to clear, so the seeded")
    note("transfers score on their own merits rather than on the last run's")
    for left in range(remaining, 0, -15):
        print(f"    {DIM}{left}s{RESET}", end="\r", flush=True)
        time.sleep(min(15, left))
    print(f"    {DIM}window clear{RESET}     ")


def main() -> None:
    print(f"\n{BOLD}seeding demo data{RESET}\n")

    status, tokens = call("/api/auth/login", method="POST",
                          body={"email": EMAIL, "password": PASSWORD})
    if status != 200:
        raise SystemExit(
            f"{RED}cannot sign in as {EMAIL}.{RESET}\n"
            f"Run: (cd services/identity && uv run python manage.py seed_demo_users)"
        )
    token = tokens["access_token"]

    # ---- account -------------------------------------------------------
    status, accounts = call("/api/accounts", token=token)
    if not accounts.get("results"):
        raise SystemExit(
            f"{RED}{EMAIL} has no account yet.{RESET}\n"
            f"Run scripts/verify_spa_api.py first, or open one in the UI."
        )
    account = accounts["results"][0]
    print(f"{BOLD}account{RESET} {account['account_number']} "
          f"available={account['balance']['available']}")

    wait_out_velocity_window(token)

    # ---- funding source ------------------------------------------------
    status, sources = call("/api/funding-sources", token=token)
    if sources.get("results"):
        source = sources["results"][0]
    else:
        _, source = call("/api/funding-sources", method="POST", token=token,
                         body={"source_type": "EXTERNAL_BANK",
                               "display_name": "HDFC ****4821",
                               "token": "tok_demo_9f21c4"})
    note(f"funding source {source['display_name']}")

    # ---- top up so there is money to move ------------------------------
    #
    # Two tranches of 90k rather than one of 180k. A single deposit over 100k
    # legitimately trips R013 (first transaction, large amount) and pushes the
    # 5-minute value velocity towards R003. The rules are doing their job; the
    # seeder just should not fight them on the way to the cases it wants.
    print(f"\n{BOLD}1. top up{RESET}")
    for _ in range(2):
        _, funded = call("/api/funding", method="POST", token=token,
                         key=str(uuid.uuid4()),
                         body={"account_id": account["id"],
                               "funding_source_id": source["id"],
                               "amount": "90000.00", "currency": "INR",
                               "rail": "BANK_DEBIT"})
        print(f"   {GREEN}{funded.get('reference')} {funded.get('status')} "
              f"score={funded.get('fraud_score')}{RESET}")

    # ---- payees --------------------------------------------------------
    print(f"\n{BOLD}2. payees{RESET}")
    suffix = uuid.uuid4().hex[:6].upper()

    _, trusted = call("/api/beneficiaries", method="POST", token=token,
                      body={"nickname": "Ravi Kulkarni", "beneficiary_type": "DOMESTIC",
                            "account_number": f"99887766{suffix}",
                            "bank_code": "HDFC0001234"})
    note(f"domestic payee {trusted.get('nickname')} {trusted.get('masked')}")

    _, overseas = call("/api/beneficiaries", method="POST", token=token,
                       body={"nickname": "Lena Fischer", "currency": "EUR",
                             "beneficiary_type": "INTERNATIONAL",
                             "account_number": f"DE8937040044{suffix}",
                             "swift_bic": "DEUTDEFF", "country": "DE"})
    note(f"international payee {overseas.get('nickname')} {overseas.get('masked')}")

    # ---- a clean transfer ----------------------------------------------
    print(f"\n{BOLD}3. an ordinary transfer{RESET}")
    _, clean = call("/api/transfers", method="POST", token=token, key=str(uuid.uuid4()),
                    body={"account_id": account["id"], "beneficiary_id": trusted["id"],
                          "amount": "2500.00", "currency": "INR", "rail": "DOMESTIC",
                          "remarks": "Dinner, split"})
    print(f"   {GREEN}{clean.get('reference')} {clean.get('status')} "
          f"score={clean.get('fraud_score')}{RESET}")

    # ---- one that lands in review --------------------------------------
    #
    # 15,000 to a payee still inside its cooling-off window fires R015 alone
    # (weight 50), which lands squarely in the 40-74 review band. Going above
    # 50,000 would stack R005 on top and block it outright -- correct, but not
    # the case being demonstrated here.
    print(f"\n{BOLD}4. payee still in cooling-off -> REVIEW{RESET}")
    _, review = call("/api/transfers", method="POST", token=token, key=str(uuid.uuid4()),
                     body={"account_id": account["id"], "beneficiary_id": trusted["id"],
                           "amount": "15000.00", "currency": "INR", "rail": "DOMESTIC",
                           "remarks": "Deposit"})
    colour = YELLOW if review.get("status") == "UNDER_REVIEW" else DIM
    print(f"   {colour}{review.get('reference')} {review.get('status')} "
          f"score={review.get('fraud_score')} case={review.get('fraud_case_id')}{RESET}")
    if review.get("status") == "UNDER_REVIEW":
        note("the analyst queue now has a case to work")

    # ---- one that is stopped outright ----------------------------------
    #
    # A large amount to a brand-new *international* payee stacks R015
    # (cooling-off, 50) on R005 (new payee, large, 45) for 95 -- past the
    # block threshold of 75.
    print(f"\n{BOLD}5. large amount, brand-new international payee -> BLOCK{RESET}")
    _, blocked = call("/api/transfers", method="POST", token=token, key=str(uuid.uuid4()),
                      body={"account_id": account["id"], "beneficiary_id": overseas["id"],
                            "amount": "95000.00", "currency": "INR",
                            "rail": "INTERNATIONAL", "remarks": "Invoice 4417"})
    colour = RED if blocked.get("status") == "BLOCKED" else DIM
    print(f"   {colour}{blocked.get('reference')} {blocked.get('status')} "
          f"score={blocked.get('fraud_score')}{RESET}")
    note(f"reason: {blocked.get('status_reason') or '-'}")

    # ---- a standing order ----------------------------------------------
    print(f"\n{BOLD}6. standing order{RESET}")
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    status, schedule = call("/api/schedules", method="POST", token=token,
                            body={"account_id": account["id"],
                                  "beneficiary_id": trusted["id"],
                                  "amount": "12000.00", "currency": "INR",
                                  "rail": "DOMESTIC", "frequency": "MONTHLY",
                                  "start_at": start.isoformat().replace("+00:00", "Z"),
                                  "max_runs": 12, "remarks": "Rent"})
    if status == 201:
        print(f"   {GREEN}monthly ₹12,000, first run {schedule['next_run_at']}{RESET}")
    else:
        print(f"   {DIM}{schedule}{RESET}")

    # ---- where things ended up -----------------------------------------
    _, final = call("/api/accounts", token=token)
    balance = final["results"][0]["balance"]
    print(f"\n{BOLD}balance{RESET} available={balance['available']} held={balance['held']}")
    if balance["held"] != "0.0000":
        note("the held amount is the transfer waiting on an analyst")

    print(f"\n{GREEN}{BOLD}demo data seeded.{RESET}")
    print(f"  {DIM}customer  asha@indbank.test{RESET}")
    print(f"  {DIM}analyst   analyst@indbank.test  -> /fraud/queue has a case{RESET}")
    print(f"  {DIM}ops       ops@indbank.test      -> /ops/queues{RESET}")
    print(f"  {DIM}admin     admin@indbank.test    -> /admin/rules{RESET}")
    print(f"  {DIM}password  {PASSWORD}{RESET}\n")


if __name__ == "__main__":
    main()
