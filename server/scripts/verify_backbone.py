#!/usr/bin/env python
"""P0 gate: prove the event backbone works across a real service boundary.

Publishes from payments-svc and asserts the event lands in audit-svc's hash
chain, having crossed: outbox -> Q2 -> HTTP -> inbox -> Q2 -> handler.

Requires audit-svc web + worker to be running:
    uv run python scripts/run_service.py audit

Then:
    uv run python scripts/verify_backbone.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

PUBLISH_SNIPPET = '''
import django, os, sys, uuid, json
sys.path.insert(0, os.getcwd())
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
django.setup()

from django.db import transaction
from platform_common.events import EventEnvelope, publish
from platform_common.events.tasks import relay_one
from platform_common.models import OutboxEvent
from platform_common.observability.context import correlation_scope

CORRELATION = "{correlation}"
AGGREGATE = "{aggregate}"

with correlation_scope(CORRELATION):
    with transaction.atomic():
        env = EventEnvelope(
            event_type="payment.settled",
            aggregate_type="transaction",
            aggregate_id=AGGREGATE,
            sequence=7,
            producer="payments",
            correlation_id=CORRELATION,
            actor={{"type": "customer", "id": "cust-1"}},
            payload={{
                "transaction_id": AGGREGATE,
                "reference": "TXN-BACKBONE-TEST",
                "amount": {{"amount": "150000.0000", "currency": "INR"}},
                "rail": "INTERNAL",
            }},
        )
        rows = publish(env)
    print("PUBLISHED", env.event_id, "to", [r.subscriber for r in rows])

# Deliver synchronously so the test does not depend on payments' own worker.
pending = OutboxEvent.objects.filter(status="PENDING")
for row in pending:
    result = relay_one(str(row.id))
    print("RELAY", row.subscriber, "->", result)

for row in OutboxEvent.objects.all():
    print("OUTBOX", row.subscriber, row.status, row.attempts, row.last_error[:80])
'''

CHECK_SNIPPET = '''
import django, os, sys
sys.path.insert(0, os.getcwd())
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
django.setup()

from audit.models import AuditLog
from audit.services import verify_chain
from platform_common.models import InboxEvent

CORRELATION = "{correlation}"

inbox = InboxEvent.objects.filter(correlation_id=CORRELATION).first()
print("INBOX", inbox.status if inbox else "MISSING", inbox.event_type if inbox else "")

row = AuditLog.objects.filter(correlation_id=CORRELATION).first()
if row:
    print("AUDITED id=%s type=%s agg=%s seq=%s" % (row.id, row.event_type, row.aggregate_id, row.sequence))
    print("HASH prev=%s row=%s" % (row.prev_hash[:16], row.row_hash[:16]))
    print("PAYLOAD_AMOUNT", row.payload.get("amount"))
else:
    print("AUDITED MISSING")

v = verify_chain()
print("CHAIN ok=%s rows=%s detail=%s" % (v.ok, v.rows_checked, v.detail))
'''


def run_in_service(service: str, snippet: str) -> str:
    service_dir = ROOT / "services" / service
    env = {**os.environ, "PYTHONPATH": str(service_dir)}
    env.pop("DB_NAME", None)  # let database_config() derive it per backend
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True, text=True, cwd=str(service_dir), env=env,
    )
    if result.returncode != 0:
        print(f"{RED}--- {service} failed ---{RESET}")
        print(result.stdout)
        print(result.stderr)
        raise SystemExit(1)
    return result.stdout


def main() -> None:
    correlation = str(uuid.uuid4())
    aggregate = str(uuid.uuid4())

    print(f"correlation_id = {correlation}\n")

    print("1. publish from payments-svc and relay over HTTP")
    output = run_in_service(
        "payments", PUBLISH_SNIPPET.format(correlation=correlation, aggregate=aggregate)
    )
    for line in output.strip().splitlines():
        print(f"   {line}")

    if "-> sent" not in output and "-> duplicate" not in output:
        print(f"\n{RED}relay did not deliver. Is audit-svc running on :8009?{RESET}")
        raise SystemExit(1)

    print("\n2. wait for audit-svc worker to dispatch the inbox row")
    for attempt in range(20):
        time.sleep(1)
        output = run_in_service(
            "audit", CHECK_SNIPPET.format(correlation=correlation)
        )
        if "AUDITED id=" in output:
            break
        print(f"   ...waiting ({attempt + 1}s)")
    else:
        print(f"\n{RED}event never reached the audit chain{RESET}")
        print(output)
        raise SystemExit(1)

    print()
    for line in output.strip().splitlines():
        print(f"   {line}")

    ok = "CHAIN ok=True" in output and "AUDITED id=" in output
    print()
    if ok:
        print(f"{GREEN}P0 GATE PASSED{RESET}  outbox -> Q2 -> HTTP -> inbox -> Q2 -> handler -> hash chain")
    else:
        print(f"{RED}P0 GATE FAILED{RESET}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
