"""SQLite files shared by concurrent workers (step ledger, Surgeon attempts, incident ids).

Two rules, learned from a CI flake (three parallel Surgeons opening one attempt log):

- WAL mode is a property of the file, set once with ``enable_wal`` (retried: switching the
  journal mode needs an exclusive lock, and SQLite answers BUSY at once instead of waiting);
  ordinary connections never switch it again.
- ``connect`` waits for other writers (busy timeout), commits on success, rolls back on error
  and always closes the connection (a ``with`` on a bare connection only commits).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_S = 30.0


@contextmanager
def connect(path: Path | str) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S)
    try:
        db.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
        with db:
            yield db
    finally:
        db.close()


def enable_wal(path: Path | str, *, attempts: int = 100, pause_s: float = 0.05) -> None:
    """Put the file in WAL mode, retrying while another connection holds it."""
    if str(path) == ":memory:":
        return
    for attempt in range(attempts):
        try:
            with connect(path) as db:
                db.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) and "busy" not in str(error).lower():
                raise
            if attempt == attempts - 1:
                raise
            time.sleep(pause_s)
