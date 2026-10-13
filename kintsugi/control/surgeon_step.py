"""The Surgeon step and its attempt-N algorithm (spec §6.6; plan §2a L2).

A Surgeon worker can die mid-attempt (S-18: ``kill -9`` of the Surgeon pool, including the
child ``claude`` process). The heartbeat timeout fails the attempt and the retry policy starts
attempt N+1 with the same idempotency key, which runs this algorithm first:

    if branch kintsugi/<INC>/<H> has a commit whose Kintsugi-Nonce was issued to an earlier
    attempt (a nonce recorded before that attempt's session started):
        kill any orphan still holding the worktree lock; adopt the commit   # side effect done
    else:
        kill the orphan; git reset --hard <base> && git clean -fdx         # disposable worktree
        record "abandoned attempt n (no commit, no checkpoint)"
        issue a NEW nonce, record it, start a new session

Nonces are recorded in ``surgeon_attempts`` *before* the session starts, so whichever attempt
follows can recognise its predecessor's commit. In Stage 8 this registry is backed by the
signed attestations in the ledger; the adoption rule is the same.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Collection
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kintsugi.control import contracts as c
from kintsugi.control.activities import StepContext
from kintsugi.control.dbconn import connect, enable_wal
from kintsugi.proof import passphrase
from kintsugi.proof.attest import parse_trailers
from kintsugi.sandbox import proc, worktree

NONCE_TRAILER = "Kintsugi-Nonce"


class AttemptLog:
    """Nonces issued per attempt and what became of each attempt (SQLite, shared by workers)."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        enable_wal(self.path)  # once per file, retried (see kintsugi.control.dbconn)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS surgeon_attempts (nonce TEXT PRIMARY KEY,"
                " incident TEXT NOT NULL, hypothesis TEXT NOT NULL, attempt INTEGER NOT NULL,"
                " state TEXT NOT NULL CHECK (state IN ('issued','committed','adopted',"
                "'abandoned')), commit_sha TEXT, note TEXT, at REAL NOT NULL)"
            )

    def _db(self) -> AbstractContextManager[sqlite3.Connection]:
        return connect(self.path)  # waits for parallel Surgeons, commits, always closes

    def issue(self, incident: str, hypothesis: str, attempt: int, nonce: str) -> None:
        with self._db() as db:
            db.execute("INSERT INTO surgeon_attempts (nonce, incident, hypothesis, attempt, state,"
                       " at) VALUES (?, ?, ?, ?, 'issued', ?)",
                       (nonce, incident, hypothesis, attempt, time.time()))  # fmt: skip

    def mark(
        self, nonce: str, state: str, *, commit_sha: str | None = None, note: str = ""
    ) -> None:
        query = ("UPDATE surgeon_attempts SET state = ?, commit_sha = COALESCE(?, commit_sha),"
                 " note = ? WHERE nonce = ?")  # fmt: skip
        with self._db() as db:
            db.execute(query, (state, commit_sha, note, nonce))

    def nonces(self, incident: str, hypothesis: str) -> list[str]:
        query = ("SELECT nonce FROM surgeon_attempts WHERE incident = ? AND hypothesis = ?"
                 " ORDER BY attempt")  # fmt: skip
        with self._db() as db:
            return [row[0] for row in db.execute(query, (incident, hypothesis))]

    def history(self, incident: str, hypothesis: str) -> list[tuple[int, str, str | None, str]]:
        with self._db() as db:
            rows = db.execute("SELECT attempt, state, commit_sha, note FROM surgeon_attempts"
                              " WHERE incident = ? AND hypothesis = ? ORDER BY attempt, at",
                              (incident, hypothesis))  # fmt: skip
            return [(int(r[0]), str(r[1]), r[2], str(r[3] or "")) for r in rows]


@dataclass(frozen=True)
class FoundCommit:
    sha: str
    nonce: str


def _git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=60, check=True).stdout


def find_commit_with_nonce(
    repo: Path, branch: str, base_sha: str, nonces: Collection[str]
) -> FoundCommit | None:
    """The newest commit on ``branch`` after ``base_sha`` carrying one of ``nonces``."""
    exists = proc.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                       f"refs/heads/{branch}"], timeout_s=30)  # fmt: skip
    if exists.returncode != 0 or not nonces:
        return None
    log = _git(repo, "log", "--format=%H%x1f%B%x1e", f"{base_sha}..refs/heads/{branch}")
    for record in log.split("\x1e"):
        if "\x1f" not in record:
            continue
        sha, message = record.strip().split("\x1f", 1)
        for value in parse_trailers(message).get(NONCE_TRAILER, []):
            if value in nonces:
                return FoundCommit(sha.strip(), value)
    return None


class SessionRunner(Protocol):
    """Runs one Surgeon session in ``wt``; returns the commit it made, if any."""

    def run(
        self,
        wt: worktree.Worktree,
        card: c.Card,
        nonce: str,
        attempt: int,
        on_start: Callable[[int], None],
    ) -> str | None: ...


class SurgeonStep:
    def __init__(
        self,
        repo: Path,
        log: AttemptLog,
        runner: SessionRunner,
        *,
        new_nonce: Callable[[], str] = passphrase.new_nonce,
    ) -> None:
        self.repo = Path(repo)
        self.log = log
        self.runner = runner
        self.new_nonce = new_nonce

    def run(self, card: c.Card, ctx: StepContext) -> c.SurgeonOutcome:
        inc, hyp = card.incident_id, card.hypothesis
        branch = worktree.branch_name(inc, hyp)
        try:
            wt = worktree.open_existing(self.repo, inc, hyp)
        except worktree.WorktreeError:
            wt = worktree.create(self.repo, inc, hyp, base=card.base_sha)
        issued = self.log.nonces(inc, hyp)
        found = find_commit_with_nonce(self.repo, branch, wt.base_sha, issued)
        if found is not None:  # the side effect already happened: never repeat it
            worktree.kill_orphan(wt)
            self.log.mark(found.nonce, "adopted", commit_sha=found.sha,
                          note=f"adopted by attempt {ctx.attempt}")  # fmt: skip
            return c.SurgeonOutcome(inc, hyp, ctx.attempt, branch, found.nonce, found.sha,
                                    diff_sha=found.sha, adopted=True,
                                    abandoned=tuple(self._abandoned(inc, hyp)))  # fmt: skip
        if issued:  # an earlier attempt died without committing
            killed = worktree.kill_orphan(wt)
            worktree.reset_clean(wt)
            for nonce in issued:
                self.log.mark(nonce, "abandoned",
                              note="no commit, no checkpoint" + (f"; killed orphan pid {killed}"
                                                                 if killed else ""))  # fmt: skip
        nonce = self.new_nonce()
        self.log.issue(inc, hyp, ctx.attempt, nonce)  # recorded before the session starts
        sha = self.runner.run(wt, card, nonce, ctx.attempt,
                              lambda pid: worktree.write_lock(wt, pid))  # fmt: skip
        worktree.release_lock(wt)
        if sha is not None:
            self.log.mark(nonce, "committed", commit_sha=sha)
        return c.SurgeonOutcome(inc, hyp, ctx.attempt, branch, nonce, sha, diff_sha=sha,
                                abandoned=tuple(self._abandoned(inc, hyp)))  # fmt: skip

    def _abandoned(self, inc: str, hyp: str) -> list[str]:
        return [f"attempt {n}: {note}" for n, state, _, note in self.log.history(inc, hyp)
                if state == "abandoned"]  # fmt: skip
