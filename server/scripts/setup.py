#!/usr/bin/env python
"""One-command bootstrap for the whole backend.

Runs every step needed to go from a fresh checkout to a working estate, in
order, and stops at the first failure. Each step is idempotent, so re-running is
always safe.

    uv run python scripts/setup.py
    uv run python scripts/setup.py --reset        # wipe the databases first
    DB_ENGINE=postgres uv run python scripts/setup.py

Missing any one of these leaves a system that *looks* fine and is not:
without `seed_rules --activate` every fraud rule sits in SHADOW mode and nothing
is ever blocked; without `register_schedules` no sweeper runs, so a lost event
is never retried and a stranded hold is never released.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def run(label: str, command: list[str], *, cwd: Path | None = None) -> bool:
    print(f"{BOLD}▸ {label}{RESET}")
    started = time.time()
    result = subprocess.run(
        command, cwd=str(cwd or ROOT), capture_output=True, text=True,
        env={**os.environ},
    )
    elapsed = time.time() - started

    if result.returncode != 0:
        print(f"  {RED}FAILED{RESET} after {elapsed:.1f}s")
        for line in (result.stdout + result.stderr).strip().splitlines()[-25:]:
            print(f"  {DIM}{line}{RESET}")
        return False

    for line in result.stdout.strip().splitlines():
        if line.strip():
            print(f"  {DIM}{line}{RESET}")
    print(f"  {GREEN}ok{RESET} ({elapsed:.1f}s)\n")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="drop and recreate the databases first")
    parser.add_argument("--shadow", action="store_true",
                        help="leave fraud rules in SHADOW mode (nothing blocks)")
    args = parser.parse_args()

    engine = os.environ.get("DB_ENGINE", "sqlite")
    print(f"\n{BOLD}backend setup{RESET}  {DIM}(database engine: {engine}){RESET}\n")

    steps: list[tuple[str, list[str], Path | None]] = [
        (
            "install dependencies",
            ["uv", "sync"],
            None,
        ),
        (
            "create one database per service" + (" (wiping first)" if args.reset else ""),
            [PYTHON, "scripts/init_databases.py"] + (["--drop"] if args.reset else []),
            None,
        ),
        (
            "apply migrations across all 10 services",
            [PYTHON, "scripts/manage_all.py", "migrate", "--quiet"],
            None,
        ),
        (
            "seed the fraud ruleset"
            + (" (SHADOW mode)" if args.shadow else " (ACTIVE)"),
            [PYTHON, "manage.py", "seed_rules"] + ([] if args.shadow else ["--activate"]),
            ROOT / "services" / "fraud",
        ),
        (
            "register Django Q2 schedules",
            [PYTHON, "scripts/manage_all.py", "register_schedules", "--quiet"],
            None,
        ),
        (
            "create the four demo sign-ins",
            [PYTHON, "manage.py", "seed_demo_users"],
            ROOT / "services" / "identity",
        ),
        (
            "verify every service boots",
            [PYTHON, "scripts/manage_all.py", "check", "--quiet"],
            None,
        ),
    ]

    for label, command, cwd in steps:
        if not run(label, command, cwd=cwd):
            print(f"{RED}{BOLD}setup stopped.{RESET} Fix the error above and re-run "
                  f"-- every step is idempotent.\n")
            sys.exit(1)

    if args.shadow:
        print(f"{YELLOW}note{RESET}: fraud rules are in SHADOW mode, so nothing will be "
              f"blocked.\n      Re-run without --shadow to activate them.\n")

    print(f"{GREEN}{BOLD}setup complete.{RESET}\n")
    print("next:")
    print(f"  {DIM}uv run python scripts/run_service.py --all{RESET}      start everything")
    print(f"  {DIM}uv run python scripts/run_service.py --core{RESET}     start the UC2 demo path")
    print(f"  {DIM}uv run python scripts/test_all.py{RESET}               run every test suite")
    print(f"  {DIM}uv run python scripts/demo_p2.py{RESET}                prove the money path")
    print()
    print("then, with the estate running:")
    print(f"  {DIM}uv run python scripts/seed_demo_data.py{RESET}         fill the four consoles")
    print(f"  {DIM}uv run python scripts/verify_spa_api.py{RESET}         check every endpoint the UI calls")
    print(f"  {DIM}cd ../client && npm install && npm run dev{RESET}      the web app on :5173")
    print()
    print(f"  {DIM}sign in as asha@indbank.test / demo-password-2026{RESET}")
    print()


if __name__ == "__main__":
    main()
