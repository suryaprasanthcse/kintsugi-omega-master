"""SQLite files shared by concurrent workers: no "database is locked" at open, no leaked handles.

Regression for omega-ci on 640e046: three parallel Surgeons opened the attempt log at once and
one failed with ``sqlite3.OperationalError: database is locked`` while switching to WAL.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from kintsugi.control.activities import SqliteLedger
from kintsugi.control.dbconn import connect, enable_wal
from kintsugi.control.surgeon_step import AttemptLog


def _together(n: int, work: Callable[[int], None]) -> list[BaseException]:
    errors: list[BaseException] = []
    start = threading.Barrier(n)

    def run(i: int) -> None:
        try:
            start.wait()
            work(i)
        except BaseException as error:  # collected for the assertion
            errors.append(error)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_parallel_surgeons_open_one_attempt_log(tmp_path: Path) -> None:
    path = tmp_path / "attempts.sqlite"

    def surgeon(i: int) -> None:
        AttemptLog(path).issue("INC-0042", f"H{i}", 1, f"nonce-{i}")

    assert _together(12, surgeon) == []
    log = AttemptLog(path)
    assert sum(len(log.nonces("INC-0042", f"H{i}")) for i in range(12)) == 12


def test_parallel_workers_share_the_step_ledger(tmp_path: Path) -> None:
    path = tmp_path / "steps.sqlite"

    def worker(i: int) -> None:
        ledger = SqliteLedger(path)
        ledger.put(f"key-{i}", b"x")
        assert ledger.get(f"key-{i}") == b"x"

    assert _together(12, worker) == []


def test_connections_are_closed_and_wal_is_set_once(tmp_path: Path) -> None:
    path = tmp_path / "shared.sqlite"
    enable_wal(path)
    with connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        db.execute("CREATE TABLE t (x INTEGER)")
    with connect(path) as db:
        db.execute("INSERT INTO t VALUES (1)")
    with connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1  # committed
    for name in ("shared.sqlite-wal", "shared.sqlite-shm"):
        (tmp_path / name).unlink(missing_ok=True)
    path.unlink()  # no handle left open (Windows refuses to delete an open file)
    enable_wal(":memory:")  # a no-op
