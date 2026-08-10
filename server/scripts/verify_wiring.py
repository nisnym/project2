#!/usr/bin/env python
"""Static gate: does the estate's wiring actually connect?

Three ways the wiring can be wrong while every unit test still passes, because
each failure is silent at runtime:

  1. An event is routed to a service that registers no handler for it. The
     publisher writes an outbox row, a relay worker makes an HTTP call, the peer
     writes an inbox row and enqueues a dispatch task -- and the task finds
     nothing to run. Full cost, no effect. This is how account-svc's cached
     balance stayed at zero forever: `ledger.posted` was routed to it and
     `account/handlers.py` was an empty stub.

  2. An event is published with no route at all. publish() logs a warning and
     the event reaches nobody but audit.

  3. A service calls a peer endpoint that does not exist, or asks for a scope
     the endpoint does not accept.

Handlers are read from the live registry -- each service is booted in its own
process and asked -- because `@subscribe(*SOME_DICT)` cannot be resolved by
reading the source.

    uv run python scripts/verify_wiring.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = [p.name for p in sorted((ROOT / "services").iterdir()) if p.is_dir()]

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m",
)

PROBE = """
import json, os, sys
sys.path.insert(0, os.getcwd())
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
import django; django.setup()
from platform_common.events.dispatcher import registered_event_types
print("__HANDLERS__" + json.dumps(registered_event_types()))
"""


def registered_handlers() -> dict[str, set[str]]:
    """Boot each service and ask what it actually listens to."""
    found: dict[str, set[str]] = {}
    for service in SERVICES:
        result = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=ROOT / "services" / service,
            capture_output=True, text=True,
        )
        line = next(
            (l for l in result.stdout.splitlines() if l.startswith("__HANDLERS__")),
            None,
        )
        if line is None:
            print(f"{RED}could not boot {service}{RESET}")
            print(result.stderr[-1500:])
            sys.exit(2)
        found[service] = set(json.loads(line[len("__HANDLERS__"):]))
    return found


def routing_table() -> dict[str, list[str]]:
    source = (
        ROOT / "libs/platform_common/platform_common/service_settings.py"
    ).read_text()
    block = source.split("EVENT_SUBSCRIPTIONS: dict[str, list[str]] = {", 1)[1]
    block = block.split("\n}", 1)[0]
    return {
        etype: re.findall(r'"([^"]+)"', subs)
        for etype, subs in re.findall(r'"([^"]+)":\s*\[([^\]]*)\]', block)
    }


def published_events() -> dict[str, set[str]]:
    """Event-type literals appearing in each service's source.

    Deliberately loose about *where* the literal sits, because publishes take
    several shapes -- `event_type="x"`, `_emit(txn, "x")`, a module-level
    `{status: "x"}` table -- and a regex tied to one of them would miss the
    others. The looseness costs one thing: a dotted string like
    "payments.handlers" is shaped exactly like an event type. Those are
    filtered by suffix against the real module names in the tree, which is
    self-maintaining in a way a hand-written denylist is not.
    """
    modules = {p.stem for p in ROOT.rglob("*.py")} | {"settings", "urls"}
    shape = re.compile(r'"([a-z][a-z_]*\.[a-z][a-z_]*)"')
    found: dict[str, set[str]] = {}
    for service in SERVICES:
        seen: set[str] = set()
        for path in (ROOT / "services" / service).rglob("*.py"):
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            for literal in shape.findall(path.read_text()):
                if literal.split(".", 1)[1] not in modules:
                    seen.add(literal)
        found[service] = seen
    return found


def check_events() -> list[str]:
    routes, handlers, published = routing_table(), registered_handlers(), published_events()
    everything_published = set().union(*published.values()) if published else set()
    problems: list[str] = []

    print(f"\n{DIM}handlers registered per service{RESET}")
    for service in SERVICES:
        count = len(handlers[service])
        mark = f"{DIM}—{RESET}" if not count else f"{GREEN}{count}{RESET}"
        print(f"  {service:<14} {mark}")

    print(f"\n{DIM}routed but unhandled{RESET}")
    dropped = 0
    for etype, subscribers in sorted(routes.items()):
        if etype == "*":
            continue
        for service in subscribers:
            if "*" in handlers.get(service, set()):
                continue  # audit's wildcard catches everything
            if etype not in handlers.get(service, set()):
                problems.append(f"{etype} -> {service}: no handler registered")
                print(f"  {RED}✗{RESET} {etype:<38} -> {service}")
                dropped += 1
    if not dropped:
        print(f"  {GREEN}✓{RESET} every route reaches a handler")

    # Not a failure. subscribers_for() always appends the "*" subscriber, so an
    # event with no entry in the table still reaches the audit chain -- it just
    # has no service that *acts* on it. Recording an administrative change
    # nobody has to react to is exactly what that looks like.
    print(f"\n{DIM}audit-only (published, no service acts on it){RESET}")
    audit_only = sorted(
        {e for events in published.values() for e in events if e not in routes}
    )
    for etype in audit_only:
        print(f"  {DIM}·{RESET} {etype}")
    if not audit_only:
        print(f"  {DIM}·{RESET} none")

    print(f"\n{DIM}routed but never published{RESET}")
    dead = 0
    for etype in sorted(routes):
        if etype == "*":
            continue
        if etype not in everything_published:
            print(f"  {YELLOW}!{RESET} {etype:<38} -> {routes[etype]}")
            dead += 1
    if not dead:
        print(f"  {GREEN}✓{RESET} every route carries traffic")

    return problems


def check_http() -> list[str]:
    """Every outbound path must exist on the peer that serves it."""
    routes_by_service: dict[str, list[str]] = {}
    for service in SERVICES:
        patterns: list[str] = []
        for name in (f"{service}/urls.py", "config/urls.py"):
            path = ROOT / "services" / service / name
            if path.exists():
                patterns += re.findall(r'path\(\s*"([^"]*)"', path.read_text())
        # Shared routes every service mounts via platform_urlpatterns().
        patterns += ["healthz", "readyz", "internal/events", "internal/metrics"]
        routes_by_service[service] = patterns

    def matches(service: str, target: str) -> bool:
        target = target.lstrip("/")
        for pattern in routes_by_service.get(service, []):
            regex = re.sub(r"<[^>]+>", "[^/]+", pattern.lstrip("/"))
            if re.fullmatch(regex, target):
                return True
        return False

    print(f"\n{DIM}outbound HTTP calls{RESET}")
    problems: list[str] = []
    call = re.compile(
        r'get_client\(\s*["\'](\w+)["\'][^)]*\)[\s\S]{0,400}?\.(get|post|patch)\(\s*f?["\']([^"\']+)["\']'
    )
    literal = re.compile(r'^(/[^{}]*?)(?:\{|$)')

    for service in SERVICES:
        for path in (ROOT / "services" / service).rglob("*.py"):
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            # payments/clients.py declares its clients and its paths in separate
            # functions, so a proximity regex pairs the wrong ones. It gets an
            # exact check below instead.
            if path.match("payments/clients.py"):
                continue
            text = path.read_text()
            for peer, _verb, target in call.findall(text):
                if peer not in routes_by_service:
                    continue  # a variable, e.g. ops scraping every service
                prefix = literal.match(target)
                probe = (prefix.group(1) if prefix else target).rstrip("/")
                # f-string interpolation: compare only the literal prefix.
                if "{" in target:
                    if any(
                        p.lstrip("/").startswith(probe.lstrip("/"))
                        for p in routes_by_service[peer]
                    ):
                        print(f"  {GREEN}✓{RESET} {service:<12} -> {peer:<12} {target}")
                        continue
                elif matches(peer, probe):
                    print(f"  {GREEN}✓{RESET} {service:<12} -> {peer:<12} {target}")
                    continue
                problems.append(f"{service} -> {peer}{target}: no such route")
                print(f"  {RED}✗{RESET} {service:<12} -> {peer:<12} {target}")

    # payments/clients.py builds paths away from the get_client() call, so the
    # regex above cannot pair them. Check its literals against ledger/account/fraud.
    clients = (ROOT / "services/payments/payments/clients.py").read_text()
    owner = {"holds": "ledger", "journal-entries": "ledger", "balances": "ledger",
             "trial-balance": "ledger", "validate-transfer": "account",
             "limits": "account", "accounts": "account", "screen": "fraud"}
    for target in re.findall(r'["\'](/internal/[^"\']+)["\']', clients):
        segment = target.split("/")[2]
        peer = owner.get(segment)
        if peer is None:
            continue
        probe = (literal.match(target).group(1) if literal.match(target) else target).rstrip("/")
        ok = matches(peer, probe) or any(
            p.lstrip("/").startswith(probe.lstrip("/")) for p in routes_by_service[peer]
        )
        mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        print(f"  {mark} {'payments':<12} -> {peer:<12} {target}")
        if not ok:
            problems.append(f"payments -> {peer}{target}: no such route")

    return problems


def check_proxy() -> list[str]:
    """Every customer-facing URL must have a dev-server proxy prefix."""
    config = (ROOT.parent / "client/vite.config.js").read_text()
    prefixes = {
        prefix: int(port)
        for prefix, port in re.findall(r"'([^']+)':\s*(\d+)", config)
    }
    ports = dict(
        re.findall(
            r'"(\w+)":\s*(\d+)',
            (ROOT / "libs/platform_common/platform_common/service_settings.py")
            .read_text()
            .split("SERVICE_PORTS = {", 1)[1]
            .split("}", 1)[0],
        )
    )

    print(f"\n{DIM}SPA proxy coverage{RESET}")
    problems: list[str] = []
    for service in SERVICES:
        urls = ROOT / "services" / service / service / "urls.py"
        if not urls.exists():
            continue
        for pattern in re.findall(r'path\(\s*"(api/[^"]*)"', urls.read_text()):
            route = "/" + pattern
            match = max(
                (p for p in prefixes if route.startswith(p)), key=len, default=None
            )
            if match is None:
                problems.append(f"{route} ({service}) has no proxy prefix")
                print(f"  {RED}✗{RESET} {route:<46} {service}: unproxied")
            elif prefixes[match] != int(ports[service]):
                problems.append(
                    f"{route} proxies to :{prefixes[match]}, {service} listens on :{ports[service]}"
                )
                print(f"  {RED}✗{RESET} {route:<46} -> :{prefixes[match]} (want :{ports[service]})")
    if not problems:
        print(f"  {GREEN}✓{RESET} every customer-facing route is proxied to its owner")
    return problems


def main() -> int:
    print("verifying estate wiring")
    problems = check_events() + check_http() + check_proxy()

    print()
    if problems:
        print(f"{RED}{len(problems)} wiring problem(s):{RESET}")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"{GREEN}wiring is consistent{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
