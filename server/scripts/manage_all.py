#!/usr/bin/env python
"""Run a Django management command across every service.

    uv run python scripts/manage_all.py migrate
    uv run python scripts/manage_all.py makemigrations
    uv run python scripts/manage_all.py check
    uv run python scripts/manage_all.py --only payments,ledger migrate

Ten services means ten manage.py invocations for anything estate-wide; doing
that by hand is how one service silently ends up un-migrated.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libs" / "platform_common"))

from platform_common.service_settings import SERVICE_PORTS  # noqa: E402

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def run(service: str, command: list[str], quiet: bool) -> tuple[bool, str]:
    manage = ROOT / "services" / service / "manage.py"
    if not manage.exists():
        return False, f"no manage.py at {manage}"

    env = {**os.environ}
    # Let platform_common.database_config() derive the name: "<svc>_db" for
    # PostgreSQL, ".data/<svc>.sqlite3" for SQLite.
    env.pop("DB_NAME", None)
    result = subprocess.run(
        [sys.executable, str(manage), *command],
        capture_output=True, text=True, env=env, cwd=str(manage.parent),
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return False, output
    return True, "" if quiet else output


def split_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Separate our own flags from the Django command.

    argparse.REMAINDER would hand `--quiet` straight to manage.py, so pull the
    flags we own out first -- from anywhere in the line, since
    `manage_all.py migrate --quiet` is the natural thing to type.
    """
    ours: list[str] = []
    theirs: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--quiet":
            ours.append(token)
        elif token == "--only":
            ours.extend(argv[index : index + 2])
            index += 1
        elif token.startswith("--only="):
            ours.append(token)
        else:
            theirs.append(token)
        index += 1
    return ours, theirs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="comma-separated subset of services")
    parser.add_argument("--quiet", action="store_true", help="only show failures")

    ours, command = split_argv(sys.argv[1:])
    args = parser.parse_args(ours)
    args.command = command

    if not args.command:
        parser.error("give a management command, e.g. migrate")

    services = list(SERVICE_PORTS)
    if args.only:
        requested = [s.strip() for s in args.only.split(",")]
        unknown = set(requested) - set(services)
        if unknown:
            parser.error(f"unknown service(s): {', '.join(sorted(unknown))}")
        services = requested

    print(f"running `{' '.join(args.command)}` across {len(services)} services\n")

    failures = []
    for service in services:
        ok, output = run(service, args.command, args.quiet)
        marker = f"{GREEN}OK  {RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"{marker} {service}")
        if output:
            for line in output.splitlines():
                print(f"     {DIM}{line}{RESET}")
        if not ok:
            failures.append(service)

    if failures:
        print(f"\n{RED}{len(failures)} service(s) failed: {', '.join(failures)}{RESET}")
        sys.exit(1)
    print(f"\n{GREEN}all {len(services)} services OK{RESET}")


if __name__ == "__main__":
    main()
