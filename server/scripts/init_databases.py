#!/usr/bin/env python
"""Prepare one database per service.

Database-per-service is the isolation boundary (ADR-006): a cross-service join
is impossible rather than merely discouraged.

**SQLite (default)** -- one file per service under ``.data/``. The isolation is
about as literal as it gets: there is not even a shared server process to reach
across. No installation required; SQLite ships with Python.

**PostgreSQL** (``DB_ENGINE=postgres``) -- one database per service, optionally
with one role each.

    uv run python scripts/init_databases.py            # create
    uv run python scripts/init_databases.py --drop     # wipe and recreate
    DB_ENGINE=postgres uv run python scripts/init_databases.py --with-roles

``--with-roles`` (PostgreSQL only) reproduces the production grant model: each
role connects to exactly one database, and UPDATE/DELETE are revoked on the
ledger postings and the audit log. That is what makes "append-only" verifiable
with ``\\dp`` rather than merely asserted. SQLite has no role system, so on
SQLite those two guarantees are application-level only.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libs" / "platform_common"))

from platform_common.service_settings import DATA_DIR, SERVICE_PORTS  # noqa: E402

GREEN, YELLOW, RESET = "\033[32m", "\033[33m", "\033[0m"

# Tables that must never be updated or deleted from, per service.
IMMUTABLE_TABLES = {
    "ledger": ["ledger_posting", "ledger_journal_entry"],
    "audit": ["audit_log"],
}


# ---------------------------------------------------------------- sqlite


def init_sqlite(drop: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"SQLite databases in {DATA_DIR}")

    for service in SERVICE_PORTS:
        path = DATA_DIR / f"{service}.sqlite3"
        if drop:
            for suffix in ("", "-wal", "-shm"):
                target = Path(str(path) + suffix)
                if target.exists():
                    target.unlink()
            print(f"  dropped  {path.name}")
        if path.exists():
            print(f"  exists   {path.name}")
        else:
            # Created for real by `migrate`; touching it here just makes the
            # layout visible before anything has run.
            path.touch()
            print(f"  created  {path.name}")

    print(f"\n{YELLOW}note{RESET}: SQLite has no roles, so the append-only guarantee on")
    print("      ledger postings and the audit log is application-level here.")
    print("      Run with DB_ENGINE=postgres --with-roles to enforce it in the DB.")
    print("\ndone. next: uv run python scripts/manage_all.py migrate")


# ------------------------------------------------------------ postgresql


def psql(sql: str, database: str = "postgres") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["psql", "-h", os.environ.get("DB_HOST", "127.0.0.1"),
         "-p", os.environ.get("DB_PORT", "5432"),
         "-U", os.environ.get("DB_SUPERUSER", os.environ.get("USER", "postgres")),
         "-d", database, "-v", "ON_ERROR_STOP=0", "-tAc", sql],
        capture_output=True, text=True,
    )


def database_exists(name: str) -> bool:
    return psql(f"SELECT 1 FROM pg_database WHERE datname = '{name}'").stdout.strip() == "1"


def init_postgres(drop: bool, with_roles: bool) -> None:
    probe = psql("SELECT 1")
    if probe.returncode != 0:
        raise SystemExit(f"cannot reach PostgreSQL:\n{probe.stderr.strip()}")

    print("PostgreSQL databases")
    for service in SERVICE_PORTS:
        name, role = f"{service}_db", f"{service}_role"

        if drop and database_exists(name):
            psql(f'DROP DATABASE "{name}"')
            print(f"  dropped  {name}")

        if database_exists(name):
            print(f"  exists   {name}")
        else:
            psql(f'CREATE DATABASE "{name}"')
            print(f"  created  {name}" if database_exists(name) else f"  FAILED   {name}")

        if with_roles:
            password = os.environ.get(f"{service.upper()}_DB_PASSWORD", f"{service}-dev-pw")
            psql(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') "
                f"THEN CREATE ROLE {role} LOGIN PASSWORD '{password}'; END IF; END $$;"
            )
            psql(f'ALTER DATABASE "{name}" OWNER TO {role}')
            psql(f'REVOKE CONNECT ON DATABASE "{name}" FROM PUBLIC')
            psql(f'GRANT CONNECT ON DATABASE "{name}" TO {role}')
            print(f"           {role} owns it, PUBLIC revoked")

    if with_roles:
        print(f"\n{YELLOW}after migrating{RESET}, lock the immutable tables:")
        print("  uv run python scripts/init_databases.py --lock-immutable")

    print("\ndone. next: uv run python scripts/manage_all.py migrate")


def lock_immutable() -> None:
    """Revoke UPDATE/DELETE on append-only tables. Run after migrate."""
    for service, tables in IMMUTABLE_TABLES.items():
        for table in tables:
            result = psql(
                f"REVOKE UPDATE, DELETE ON {table} FROM {service}_role;",
                database=f"{service}_db",
            )
            status = "locked" if result.returncode == 0 else f"FAILED: {result.stderr.strip()}"
            print(f"  {service}.{table}: {status}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drop", action="store_true")
    parser.add_argument("--with-roles", action="store_true", help="PostgreSQL only")
    parser.add_argument("--lock-immutable", action="store_true",
                        help="PostgreSQL only; run after migrate")
    args = parser.parse_args()

    engine = os.environ.get("DB_ENGINE", "sqlite").lower()

    if args.lock_immutable:
        if engine == "sqlite":
            raise SystemExit("--lock-immutable requires DB_ENGINE=postgres")
        return lock_immutable()

    if engine in ("postgres", "postgresql"):
        init_postgres(args.drop, args.with_roles)
    else:
        if args.with_roles:
            print(f"{YELLOW}--with-roles is ignored on SQLite{RESET}\n")
        init_sqlite(args.drop)


if __name__ == "__main__":
    main()
