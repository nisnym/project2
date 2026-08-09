#!/usr/bin/env python
"""Run a service locally: web server + Q2 worker, no Docker required.

    uv run python scripts/run_service.py payments
    uv run python scripts/run_service.py --all
    uv run python scripts/run_service.py --core        # the UC2 demo path
    uv run python scripts/run_service.py audit --no-worker

Each service is two processes (LLD s12.1): gunicorn/runserver for the API and
`manage.py qcluster` for the workers. They scale on different signals in
production, so they are separate here too.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libs" / "platform_common"))

from platform_common.service_settings import SERVICE_PORTS  # noqa: E402

# Enough to demonstrate UC2 end to end.
CORE = ["identity", "account", "ledger", "payments", "fraud", "notification", "audit"]

COLORS = ["\033[36m", "\033[32m", "\033[33m", "\033[35m", "\033[34m", "\033[91m",
          "\033[92m", "\033[93m", "\033[95m", "\033[96m"]
RESET = "\033[0m"


def spawn(service: str, kind: str, color: str, logdir: Path) -> subprocess.Popen:
    service_dir = ROOT / "services" / service
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "SERVICE_NAME": service}
    env.pop("DB_NAME", None)  # let database_config() derive it per backend
    if kind == "web":
        command = [sys.executable, "manage.py", "runserver",
                   f"127.0.0.1:{SERVICE_PORTS[service]}", "--noreload"]
    else:
        command = [sys.executable, "manage.py", "qcluster"]

    logfile = (logdir / f"{service}-{kind}.log").open("w")
    process = subprocess.Popen(
        command, cwd=str(service_dir), env=env,
        stdout=logfile, stderr=subprocess.STDOUT,
    )
    label = f"{service}-{kind}"
    print(f"  {color}{label:22s}{RESET} pid={process.pid:<7} -> {logfile.name}")
    return process


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("services", nargs="*", help="service names")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--core", action="store_true", help="the UC2 demo subset")
    parser.add_argument("--no-worker", action="store_true", help="web process only")
    parser.add_argument("--no-web", action="store_true", help="worker process only")
    args = parser.parse_args()

    if args.all:
        targets = list(SERVICE_PORTS)
    elif args.core:
        targets = CORE
    elif args.services:
        targets = args.services
    else:
        parser.error("give service names, or --all / --core")

    unknown = set(targets) - set(SERVICE_PORTS)
    if unknown:
        parser.error(f"unknown service(s): {', '.join(sorted(unknown))}")

    logdir = ROOT / ".logs"
    logdir.mkdir(exist_ok=True)

    print(f"starting {len(targets)} service(s); logs in {logdir}\n")
    processes: list[tuple[str, subprocess.Popen]] = []
    for index, service in enumerate(targets):
        color = COLORS[index % len(COLORS)]
        if not args.no_web:
            processes.append((f"{service}-web", spawn(service, "web", color, logdir)))
        if not args.no_worker:
            processes.append((f"{service}-worker", spawn(service, "worker", color, logdir)))

    print(f"\nports: " + ", ".join(f"{s}:{SERVICE_PORTS[s]}" for s in targets))
    print("Ctrl-C to stop all.\n")

    def shutdown(*_):
        print("\nstopping...")
        for name, process in processes:
            if process.poll() is None:
                process.terminate()
        deadline = time.time() + 10
        for name, process in processes:
            remaining = max(0, deadline - time.time())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                print(f"  killing {name}")
                process.kill()
        print("stopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Surface a process that dies on its own rather than pretending all is well.
    while True:
        time.sleep(1)
        for name, process in processes:
            if process.poll() is not None:
                print(f"\n{name} exited with code {process.returncode}; "
                      f"see {logdir / (name.replace('-web', '-web').replace('-worker', '-worker') + '.log')}")
                shutdown()


if __name__ == "__main__":
    main()
