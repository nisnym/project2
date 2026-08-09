#!/usr/bin/env python
"""Run every test suite in the repo.

Each service is its own Django project with its own settings, so a single
pytest invocation cannot serve them all -- each location is run with its own
rootdir and DJANGO_SETTINGS_MODULE.

    uv run python scripts/test_all.py
    uv run python scripts/test_all.py --only payments,ledger
    uv run python scripts/test_all.py -k idempotency
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

PYTEST = ROOT / ".venv" / "bin" / "pytest"
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def locations() -> list[tuple[str, Path]]:
    found = [("platform_common", ROOT / "libs" / "platform_common")]
    for service in SERVICE_PORTS:
        directory = ROOT / "services" / service
        if (directory / "pytest.ini").exists() and any(
            (directory / "tests").glob("test_*.py")
        ):
            found.append((service, directory))
    return found


def run(name: str, directory: Path, extra: list[str]) -> tuple[bool, str, str]:
    env = {**os.environ}
    env.pop("DB_NAME", None)

    result = subprocess.run(
        [str(PYTEST), *extra], cwd=str(directory), env=env,
        capture_output=True, text=True,
    )
    output = (result.stdout + result.stderr).strip()
    summary = output.splitlines()[-1] if output else "no output"
    return result.returncode == 0, summary, output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="comma-separated subset")
    parser.add_argument("-k", dest="keyword", help="pytest -k expression")
    parser.add_argument("-v", dest="verbose", action="store_true")
    args = parser.parse_args()

    targets = locations()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        targets = [(n, d) for n, d in targets if n in wanted]

    extra: list[str] = []
    if args.keyword:
        extra += ["-k", args.keyword]

    print(f"{BOLD}running {len(targets)} test suite(s){RESET}\n")

    failures = []
    for name, directory in targets:
        ok, summary, output = run(name, directory, extra)
        marker = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"{marker} {name:18s} {DIM}{summary}{RESET}")
        if not ok or args.verbose:
            for line in output.splitlines():
                print(f"     {line}")
        if not ok:
            failures.append(name)

    print()
    if failures:
        print(f"{RED}{BOLD}{len(failures)} suite(s) failed: {', '.join(failures)}{RESET}")
        sys.exit(1)
    print(f"{GREEN}{BOLD}all suites passed{RESET}")


if __name__ == "__main__":
    main()
