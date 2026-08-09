#!/usr/bin/env python
"""Exercise every endpoint the SPA calls, over real HTTP, as a real user.

Start the estate and seed the demo users first:

    uv run python scripts/run_service.py --all
    (cd services/identity && uv run python manage.py seed_demo_users)
    uv run python scripts/verify_spa_api.py

This is the gate that catches the class of bug a unit test cannot: a route that
was never wired, a permission class that rejects the role the UI signs in as, a
response field the UI reads but the serialiser never emits.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

# Mirrors client/vite.config.js. If these two ever disagree, the SPA breaks in a
# way no backend test would notice -- so the check is kept honest by using the
# same path prefixes the browser uses.
ROUTES = {
    "/api/auth": 8001,
    "/api/onboarding": 8002,
    "/api/accounts": 8004,
    "/api/beneficiaries": 8004,
    "/api/limit-policies": 8004,
    "/api/transactions": 8005,
    "/api/transfers": 8005,
    "/api/funding-sources": 8005,
    "/api/funding": 8005,
    "/api/schedules": 8005,
    "/api/staff/transactions": 8005,
    "/api/fraud": 8007,
    "/api/notifications": 8008,
    "/api/audit": 8009,
    "/api/ops": 8010,
}

PASSWORD = "demo-password-2026"

passed = failed = 0


def resolve(path: str) -> str:
    for prefix in sorted(ROUTES, key=len, reverse=True):
        if path.startswith(prefix):
            return f"http://127.0.0.1:{ROUTES[prefix]}{path}"
    raise SystemExit(f"no service owns {path!r} -- the SPA proxy would 404")


def call(path, *, method="GET", token=None, body=None, idempotency_key=None):
    request = urllib.request.Request(resolve(path), method=method)
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if idempotency_key:
        request.add_header("Idempotency-Key", idempotency_key)
    if body is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(body).encode()

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"raw": raw.decode()[:200]}
    except urllib.error.URLError as error:
        return 0, {"error": {"message": str(error.reason)}}


def check(label, condition, note=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  {GREEN}pass{RESET} {label}" + (f" {DIM}{note}{RESET}" if note else ""))
    else:
        failed += 1
        print(f"  {RED}FAIL{RESET} {label} {DIM}{note}{RESET}")
    return bool(condition)


def section(title):
    print(f"\n{BOLD}{title}{RESET}")


def login(email):
    status, body = call("/api/auth/login", method="POST",
                        body={"email": email, "password": PASSWORD})
    if status != 200:
        print(f"{RED}could not sign in as {email}: {status} {body}{RESET}")
        print(f"{YELLOW}run: (cd services/identity && uv run python manage.py "
              f"seed_demo_users){RESET}")
        raise SystemExit(1)
    return body["access_token"]


def main() -> None:
    section("auth — the four demo sign-ins the login screen advertises")
    tokens = {}
    for role, email in [
        ("customer", "asha@indbank.test"),
        ("analyst", "analyst@indbank.test"),
        ("ops", "ops@indbank.test"),
        ("admin", "admin@indbank.test"),
    ]:
        tokens[role] = login(email)
        status, me = call("/api/auth/me", token=tokens[role])
        check(f"{role} signs in and /me resolves", status == 200, me.get("role", ""))

    customer, analyst, ops, admin = (
        tokens["customer"], tokens["analyst"], tokens["ops"], tokens["admin"]
    )

    section("customer — accounts")
    status, accounts = call("/api/accounts", token=customer)
    check("GET /api/accounts", status == 200)
    results = accounts.get("results", [])

    if not results:
        # First run: the customer has no account yet, so drive UC1 end to end.
        print(f"  {DIM}no account yet -- running onboarding{RESET}")
        # The KYC simulator scores deterministically from
        # sha256("name|dob|national_id"), so these three values are chosen to
        # clear IDENTITY_PASS rather than left to chance -- otherwise the gate
        # would pass or fail depending on which demo name was typed.
        status, app = call("/api/onboarding/applications", method="POST", token=customer,
                           body={"customer_info": {
                               "full_name": "Asha Menon", "date_of_birth": "1992-04-11",
                               "national_id": "ABCDE1016F", "nationality": "IN",
                               "employment_status": "EMPLOYED", "annual_income": 900000,
                               # The SPA hashes each file in the browser and sends
                               # only the digest. Without documents kyc-svc has no
                               # document score and correctly routes to
                               # MANUAL_REVIEW, so the gate must send them too.
                               "documents": [
                                   {"doc_type": "PASSPORT", "filename": "passport.jpg",
                                    "sha256": "a" * 64},
                                   {"doc_type": "UTILITY_BILL", "filename": "bill.pdf",
                                    "sha256": "b" * 64},
                               ],
                           }})
        check("POST /api/onboarding/applications", status == 201)
        if status == 201:
            status, _ = call(f"/api/onboarding/applications/{app['id']}/submit",
                             method="POST", token=customer, body={})
            check("POST .../submit", status == 202)
            import time
            for _ in range(40):
                time.sleep(0.75)
                _, state = call(f"/api/onboarding/applications/{app['id']}", token=customer)
                if state.get("status") in ("ACCOUNT_OPENED", "KYC_FAILED", "MANUAL_REVIEW"):
                    break
            check("application reaches ACCOUNT_OPENED",
                  state.get("status") == "ACCOUNT_OPENED", state.get("status", "?"))
        status, accounts = call("/api/accounts", token=customer)
        results = accounts.get("results", [])

    if not check("customer has at least one account", bool(results)):
        raise SystemExit(1)

    account = results[0]
    check("account exposes a live balance", "available" in account.get("balance", {}),
          f"available={account['balance'].get('available')}")
    check("balance is a string, not a float",
          isinstance(account["balance"]["available"], str))

    status, detail = call(f"/api/accounts/{account['id']}", token=customer)
    check("GET /api/accounts/{id} returns limits",
          status == 200 and "limits" in detail,
          f"per_txn_max={detail.get('limits', {}).get('per_txn_max')}")

    section("customer — funding source and top-up")
    status, sources = call("/api/funding-sources", token=customer)
    check("GET /api/funding-sources", status == 200)
    if not sources.get("results"):
        status, source = call("/api/funding-sources", method="POST", token=customer,
                              body={"source_type": "EXTERNAL_BANK",
                                    "display_name": "HDFC ****4821",
                                    "token": "tok_demo_9f21c4"})
        check("POST /api/funding-sources", status == 201)
    else:
        source = sources["results"][0]

    status, rejected = call("/api/funding-sources", method="POST", token=customer,
                            body={"source_type": "DEBIT_CARD", "display_name": "Card",
                                  "token": "4111111111111111"})
    check("a raw PAN is refused", status == 400,
          rejected.get("error", {}).get("code", ""))

    status, funded = call("/api/funding", method="POST", token=customer,
                          idempotency_key=str(uuid.uuid4()),
                          body={"account_id": account["id"],
                                "funding_source_id": source["id"],
                                "amount": "50000.00", "currency": "INR",
                                "rail": "BANK_DEBIT"})
    check("POST /api/funding", status == 201, funded.get("status", ""))

    section("customer — payee and transfer")
    status, payees = call("/api/beneficiaries?status=ACTIVE", token=customer)
    check("GET /api/beneficiaries", status == 200)

    status, bad = call("/api/beneficiaries", method="POST", token=customer,
                       body={"nickname": "Bad IFSC", "beneficiary_type": "DOMESTIC",
                             "account_number": "9988776655", "bank_code": "nope"})
    check("a malformed IFSC is refused at the edge", status == 400)

    if payees.get("results"):
        payee = payees["results"][0]
    else:
        status, payee = call("/api/beneficiaries", method="POST", token=customer,
                             body={"nickname": "Ravi", "beneficiary_type": "DOMESTIC",
                                   "account_number": "9988776655",
                                   "bank_code": "HDFC0001234"})
        check("POST /api/beneficiaries", status == 201)

    check("beneficiary response masks the account number",
          payee.get("account_number", "").startswith("****"),
          payee.get("account_number", ""))

    key = str(uuid.uuid4())
    status, txn = call("/api/transfers", method="POST", token=customer,
                       idempotency_key=key,
                       body={"account_id": account["id"], "beneficiary_id": payee["id"],
                             "amount": "1500.00", "currency": "INR", "rail": "DOMESTIC",
                             "remarks": "SPA verification"})
    check("POST /api/transfers", status == 201,
          f"{txn.get('reference')} {txn.get('status')} score={txn.get('fraud_score')}")

    # The single most important behaviour in the product.
    status, replay = call("/api/transfers", method="POST", token=customer,
                          idempotency_key=key,
                          body={"account_id": account["id"], "beneficiary_id": payee["id"],
                                "amount": "1500.00", "currency": "INR", "rail": "DOMESTIC",
                                "remarks": "SPA verification"})
    check("replaying the Idempotency-Key returns the original, not a second payment",
          replay.get("id") == txn.get("id"), replay.get("reference", ""))

    status, conflict = call("/api/transfers", method="POST", token=customer,
                            idempotency_key=key,
                            body={"account_id": account["id"],
                                  "beneficiary_id": payee["id"],
                                  "amount": "9999.00", "currency": "INR",
                                  "rail": "DOMESTIC"})
    check("same key with a different body is a 409, not a silent replay",
          status == 409, conflict.get("error", {}).get("code", ""))

    status, missing_key = call("/api/transfers", method="POST", token=customer,
                               body={"account_id": account["id"],
                                     "beneficiary_id": payee["id"],
                                     "amount": "10.00", "currency": "INR",
                                     "rail": "DOMESTIC"})
    check("a transfer with no Idempotency-Key is refused", status == 400,
          missing_key.get("error", {}).get("code", ""))

    section("customer — history")
    status, listing = call("/api/transactions?limit=5", token=customer)
    check("GET /api/transactions", status == 200,
          f"{len(listing.get('results', []))} rows")

    status, summary = call("/api/transactions/summary", token=customer)
    check("GET /api/transactions/summary", status == 200 and "by_status" in summary)

    status, one = call(f"/api/transactions/{txn['id']}", token=customer)
    check("GET /api/transactions/{id} includes the saga trace",
          status == 200 and isinstance(one.get("steps"), list),
          f"{len(one.get('steps', []))} steps")

    section("customer — schedules and notifications")
    status, schedules = call("/api/schedules", token=customer)
    check("GET /api/schedules", status == 200)

    status, no_end = call("/api/schedules", method="POST", token=customer,
                          body={"account_id": account["id"],
                                "beneficiary_id": payee["id"], "amount": "100.00",
                                "currency": "INR", "rail": "DOMESTIC",
                                "frequency": "MONTHLY",
                                "start_at": "2030-01-01T10:00:00Z"})
    check("a schedule with no stopping condition is refused", status == 400)

    status, notifications = call("/api/notifications", token=customer)
    check("GET /api/notifications", status == 200,
          f"unread={notifications.get('unread_count')}")

    section("authorisation — a customer token must not reach staff endpoints")
    for path in ("/api/fraud/cases", "/api/ops/queues", "/api/audit/logs",
                 "/api/fraud/rules"):
        status, _ = call(path, token=customer)
        check(f"customer is refused {path}", status == 403, f"got {status}")

    section("fraud analyst")
    status, cases = call("/api/fraud/cases?status=OPEN", token=analyst)
    check("GET /api/fraud/cases", status == 200,
          f"{len(cases.get('results', []))} open")

    status, stats = call("/api/fraud/stats?days=7", token=analyst)
    check("GET /api/fraud/stats", status == 200,
          f"screened={stats.get('screened')}")

    if cases.get("results"):
        case_id = cases["results"][0]["id"]
        status, case = call(f"/api/fraud/cases/{case_id}", token=analyst)
        check("GET /api/fraud/cases/{id} explains the decision",
              status == 200 and "reason_codes" in case.get("decision", {}),
              f"score={case.get('decision', {}).get('score')}")
    else:
        print(f"  {DIM}no open cases to inspect{RESET}")

    section("operations")
    status, queues = call("/api/ops/queues", token=ops)
    check("GET /api/ops/queues", status == 200,
          f"{len(queues.get('services', []))} services, "
          f"{len(queues.get('unhealthy', []))} unhealthy")

    status, failures = call("/api/ops/failures?status=OPEN", token=ops)
    check("GET /api/ops/failures", status == 200,
          f"{len(failures.get('results', []))} open")

    section("administrator")
    status, rules = call("/api/fraud/rules", token=admin)
    check("GET /api/fraud/rules", status == 200,
          f"{len(rules.get('results', []))} rules")

    status, thresholds = call("/api/fraud/thresholds", token=admin)
    check("GET /api/fraud/thresholds", status == 200,
          f"allow<{thresholds.get('allow_below')} block>={thresholds.get('block_at_or_above')}")
    check("thresholds expose pre-computed bands", "bands" in thresholds)

    status, bad_bands = call("/api/fraud/thresholds", method="PATCH", token=admin,
                             body={"allow_below": 90, "block_at_or_above": 20})
    check("impossible thresholds are refused", status == 400)

    status, dry = call("/api/fraud/rules/dry-run", method="POST", token=admin,
                       body={"condition": {"fact": "amount", "op": "gt", "value": 1000},
                             "sample_days": 30})
    check("POST /api/fraud/rules/dry-run replays real traffic", status == 200,
          f"evaluated={dry.get('evaluated')} would_fire={dry.get('would_fire')}")

    status, broken = call("/api/fraud/rules/dry-run", method="POST", token=admin,
                          body={"condition": {"fact": "amount", "op": "SYSTEM", "value": 1}})
    check("an invalid rule condition is rejected", status == 400,
          broken.get("error", {}).get("code", ""))

    status, policies = call("/api/limit-policies", token=admin)
    check("GET /api/limit-policies", status == 200,
          f"{len(policies.get('results', []))} policies")

    if policies.get("results"):
        policy = policies["results"][0]
        status, out_of_order = call("/api/limit-policies", method="PATCH", token=admin,
                                    body={"id": policy["id"], "per_txn_max": "999999999",
                                          "daily_max": "1000"})
        check("limits that violate per_txn <= daily are refused", status == 400)

    section("audit")
    status, logs = call("/api/audit/logs?limit=5", token=admin)
    check("GET /api/audit/logs", status == 200,
          f"{len(logs.get('results', []))} entries")

    status, audit_stats = call("/api/audit/stats", token=admin)
    check("GET /api/audit/stats", status == 200, f"total={audit_stats.get('total')}")

    status, chain = call("/api/audit/chain?verify=true", token=admin)
    check("the hash chain verifies", status == 200 and chain.get("verified") is True,
          f"rows_checked={chain.get('rows_checked')}")

    if txn.get("correlation_id"):
        status, trace = call(f"/api/audit/trace/{txn['correlation_id']}", token=admin)
        check("the transfer is reassembled across services from its correlation id",
              status == 200 and trace.get("count", 0) > 0,
              f"{trace.get('count')} entries from {trace.get('services')}")

    section("staff read path")
    status, staff_view = call(f"/api/staff/transactions/{txn['id']}", token=ops)
    check("ops can read a transaction through the staff route", status == 200,
          staff_view.get("reference", ""))

    print(f"\n{BOLD}{passed} passed, {failed} failed{RESET}")
    if failed:
        print(f"{RED}{BOLD}SPA API GATE FAILED{RESET}\n")
        sys.exit(1)
    print(f"{GREEN}{BOLD}SPA API GATE PASSED{RESET} "
          "- every endpoint the four consoles call works over real HTTP\n")


if __name__ == "__main__":
    main()
