"""A tiny JSON-file store — one file per aggregate, owned by one service.

Two properties matter for this product:

1. Nothing is hardcoded. Every customer, transaction and budget lives in
   data/*.json. Swap the files and the entire application — balances,
   budgets, insights, chat answers — describes a different person.
2. Edits are picked up live. Each read checks the file's mtime and reloads
   if it changed, so a judge can edit the ledger in a text editor and hit
   refresh without restarting anything.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .config import settings
from .errors import AppError


class JsonStore:
    def __init__(self, filename: str, *, key: str = "id") -> None:
        self.path: Path = settings.data_path / filename
        self.key = key
        self._rows: list[dict[str, Any]] = []
        self._mtime: float = -1.0
        self._lock = threading.Lock()
        self.load()

    # -- loading -------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            raise AppError(
                f"Data file missing: {self.path.name}",
                code="data_missing",
                status_code=500,
                hint=f"Expected it at {self.path}. Check DATA_DIR in .env.",
            )
        with self._lock:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise AppError(
                    f"{self.path.name} must contain a JSON array",
                    code="data_invalid",
                    status_code=500,
                )
            self._rows = raw
            self._mtime = self.path.stat().st_mtime

    def _refresh_if_changed(self) -> None:
        try:
            if self.path.stat().st_mtime != self._mtime:
                self.load()
        except FileNotFoundError:
            pass  # keep serving the last good copy rather than 500 mid-demo

    # -- reading -------------------------------------------------------

    def all(self) -> list[dict[str, Any]]:
        self._refresh_if_changed()
        return [dict(r) for r in self._rows]

    def find(self, **equals: Any) -> list[dict[str, Any]]:
        return [r for r in self.all() if all(r.get(k) == v for k, v in equals.items())]

    def where(self, predicate: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        return [r for r in self.all() if predicate(r)]

    def get(self, value: Any) -> Optional[dict[str, Any]]:
        for row in self.all():
            if row.get(self.key) == value:
                return row
        return None

    # -- writing -------------------------------------------------------

    def append(self, row: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._rows.append(row)
            if settings.persist_writes:
                self._flush_locked()
        return row

    def upsert(self, row: dict[str, Any], **match: Any) -> dict[str, Any]:
        with self._lock:
            for i, existing in enumerate(self._rows):
                if all(existing.get(k) == v for k, v in match.items()):
                    self._rows[i] = {**existing, **row}
                    if settings.persist_writes:
                        self._flush_locked()
                    return self._rows[i]
            self._rows.append(row)
            if settings.persist_writes:
                self._flush_locked()
            return row

    def _flush_locked(self) -> None:
        self.path.write_text(
            json.dumps(self._rows, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self._mtime = self.path.stat().st_mtime

    # -- misc ----------------------------------------------------------

    def next_id(self, prefix: str) -> str:
        existing = {r.get(self.key) for r in self.all()}
        n = len(existing) + 1
        while f"{prefix}{n:04d}" in existing:
            n += 1
        return f"{prefix}{n:04d}"

    def count(self) -> int:
        return len(self.all())


def sum_amounts(rows: Iterable[dict[str, Any]]) -> float:
    return round(sum(float(r.get("amount", 0)) for r in rows), 2)
