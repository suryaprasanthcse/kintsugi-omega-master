"""The W wiring rehearsal (Stage 13a step 7, offline): T0 → seal → deliver → anchors → audit.

A disclosed, disposable, **deterministic** run of the W runtime's own wiring on a scratch practice
monorepo outside every repository. Nothing here is a real sabotage, a real key or a real host:

1. **Practice monorepo** with one disclosed practice host, ``hosts/practice`` (``fetch_all`` and
   its tests), a local bare ``origin`` and a placeholder holdout manifest.
2. **Before T0**: a practice spec (``code.regression.exception_swallow``, the Stage 8 test spec's
   shape, never S-17/18/19) is sealed into an age store outside the repository; its commitment
   record goes into the T0 commit (A-68).
3. **T0**: a rehearsal attestation key (never the T0 key, A-4); ``attest.pub`` committed;
   ``kintsugi t0 genesis`` writes the window record and seq 1; the T0 commit is pushed.
4. **W**: the crew comes from ``kintsugi.crew.runtime.build`` (the factory W uses), with the
   sabotage lane on the real upstream lane (``kintsugi.crew.upstream``). Two stand-ins, both
   disclosed: a notary session that applies the patch with ``git apply`` (no model, so the
   commits carry no Entire checkpoint) and local pull requests whose "CI" is the practice
   host's own test suite at the branch head and whose merge is ``git merge --no-ff`` pushed to
   the bare origin. The crew seals (the ledger's ``sabotage/sealed``), delivers after
   ``not_before`` (``sabotage/delivered``) and anchors the ledger (pushed fast-forward).
5. **After W**: ``t1`` and the reveal are committed; ``kintsugi audit`` runs C1 (chain and
   anchors) and C6 (commitment, order, reveal, honeytoken).

Out of scope offline, and left to the Stage 13b dress rehearsal: C2–C5 need Entire checkpoints
from real sessions; C7, C12 need GitHub; C8–C10 need neatlogs; C11, C14 need antibodies and
self-repairs in W; C13 needs a sealed holdout, which the practice host does not have.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kintsugi.crew import claude_session as cs
from kintsugi.crew.factory import SabotageLane
from kintsugi.crew.notary import Notary
from kintsugi.crew.runtime import RuntimeConfig, build
from kintsugi.crew.saboteur import Saboteur, compose
from kintsugi.crew.upstream import UpstreamConfig, UpstreamLane
from kintsugi.gates.holdout import _inside_git_worktree
from kintsugi.proof import attest, audit, sealed
from kintsugi.proof.canonical import canonicalize
from kintsugi.sandbox import proc

HOST = "practice"
SPEC_ID = "S-P1"
CHECKS = ("C1", "C6")
IDENTITY = {"user.name": "Surya Prasanth R", "user.email": "suryaprasanthcse@gmail.com"}
FETCH = "hosts/practice/practice/fetch.py"
HEALTHY = '''"""Practice host (disclosed, Stage 13a rehearsal): fetch with one retry on timeout."""


def fetch_all(client):
    try:
        return client.fetch()
    except TimeoutError:
        raise
'''
TESTS = """from practice.fetch import fetch_all


class Client:
    def fetch(self):
        return [1, 2]


def test_fetch_all_returns_what_the_client_fetched():
    assert fetch_all(Client()) == [1, 2]
"""
PATCH = """--- a/hosts/practice/practice/fetch.py
+++ b/hosts/practice/practice/fetch.py
@@ -5,4 +5,4 @@ def fetch_all(client):
     try:
         return client.fetch()
-    except TimeoutError:
-        raise
+    except Exception:
+        return []
"""
FRAMING = "Simplify error handling in fetch_all"
_DIFF = re.compile(r"```diff\n(.*?)\n```", re.DOTALL)


class RehearsalError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=120, check=True).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class OfflineNotarySession:
    """Disclosed stand-in for a notary's headless session: applies the prompt's patch exactly
    and reports one Edit call per changed file. No model, so no Entire checkpoint."""

    def __call__(self, request: cs.SessionRequest, on_start: Any = None) -> cs.SessionResult:
        found = _DIFF.search(request.prompt)
        if found is None:
            raise RehearsalError("the notary prompt carries no patch")
        cwd = Path(request.cwd)
        with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8",
                                         newline="\n") as handle:  # fmt: skip
            handle.write(found.group(1) + "\n")
        try:
            proc.run(["git", "-C", str(cwd), "apply", "--whitespace=nowarn", handle.name],
                     timeout_s=60, check=True)  # fmt: skip
        finally:
            Path(handle.name).unlink(missing_ok=True)
        status = proc.run(["git", "-C", str(cwd), "status", "--porcelain", "-uall"],
                          timeout_s=60, check=True).stdout.split("\n")  # fmt: skip
        paths = [line[3:].strip() for line in status if line.strip()]
        stream = cs.StreamSummary(session_id=request.session_id)
        stream.tool_uses = [{"name": "Edit", "input": {"file_path": str(cwd / p)}} for p in paths]
        stream.result = {"result": "applied (offline stand-in)", "total_cost_usd": 0.0}
        return cs.SessionResult(request, stream, 0, False, False, None, None)


@dataclass
class LocalPulls:
    """Disclosed stand-in for GitHub pull requests on the practice monorepo: the "CI" is the
    practice host's own tests at the branch head; a merge is ``--no-ff`` pushed to origin."""

    repo: Path
    remote: str = "origin"
    pulls: dict[int, dict[str, str]] = field(default_factory=dict)

    def pr_find(self, head: str) -> dict[str, Any] | None:
        return next(({"number": n, "url": p["url"]} for n, p in self.pulls.items()
                     if p["head"] == head), None)  # fmt: skip

    def pr_create(self, base: str, head: str, title: str, body: str) -> dict[str, Any]:
        number = len(self.pulls) + 1
        self.pulls[number] = {"base": base, "head": head, "title": title,
                              "url": f"local://pull/{number}"}  # fmt: skip
        return {"number": number, "url": self.pulls[number]["url"]}

    def pr_checks(self, number: int) -> list[dict[str, Any]]:
        head = self.pulls[number]["head"]
        _git(self.repo, "fetch", "-q", self.remote, f"+refs/heads/{head}:refs/pulls/{number}")
        with tempfile.TemporaryDirectory() as scratch:
            checkout = Path(scratch) / "ci"
            _git(
                self.repo,
                "worktree",
                "add",
                "-q",
                "--detach",
                str(checkout),
                f"refs/pulls/{number}",
            )
            argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    "hosts/practice/tests"]  # fmt: skip
            env = {"PYTHONPATH": str(checkout / "hosts" / "practice")}
            try:
                result = proc.run(argv, cwd=checkout, timeout_s=300, extra_env=env)
            finally:
                _git(self.repo, "worktree", "remove", "--force", str(checkout))
        return [{"name": "practice / test", "bucket": "pass" if result.ok else "fail"}]

    def pr_merge(self, number: int, method: str) -> str | None:
        pull = self.pulls[number]
        _git(self.repo, "fetch", "-q", self.remote, f"+refs/heads/{pull['base']}:refs/pulls/base")
        _git(self.repo, "checkout", "-q", "--detach", "refs/pulls/base")
        message = f"Merge pull request #{number} from {pull['head']}"
        _git(self.repo, "merge", "--no-ff", "-q", "-m", message, f"refs/pulls/{number}")
        merged = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "push", "-q", self.remote, f"{merged}:refs/heads/{pull['base']}")
        _git(self.repo, "checkout", "-q", "-B", pull["base"], merged)
        return merged


@dataclass
class WRehearsal:
    run: str
    root: Path  # outside every repository
    delay_s: float = 2.0  # not_before = seal time + delay
    log: Callable[[str], None] = print

    @property
    def repo(self) -> Path:
        return self.root / "repo"


def prepare(cfg: WRehearsal) -> None:
    if _inside_git_worktree(cfg.root / "x"):
        raise RehearsalError(f"{cfg.root} is inside a git worktree: rehearsals run outside")
    if cfg.repo.exists():
        raise RehearsalError(f"{cfg.repo} exists: every rehearsal starts fresh")
    origin = cfg.root / "origin.git"
    origin.mkdir(parents=True)
    _git(origin, "init", "-q", "--bare", "-b", "main")
    cfg.repo.mkdir()
    _git(cfg.repo, "init", "-q", "-b", "main")
    for key, value in {**IDENTITY, "core.autocrlf": "false"}.items():
        _git(cfg.repo, "config", key, value)
    files = {FETCH: HEALTHY, "hosts/practice/practice/__init__.py": "",
             "hosts/practice/tests/test_fetch.py": TESTS,
             ".kintsugi/holdout.sha256": "# practice: no holdout (disclosed)\n"}  # fmt: skip
    for relative, text in files.items():
        path = cfg.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    _commit(cfg.repo, "practice: the healthy practice host")
    _git(cfg.repo, "remote", "add", "origin", str(origin))
    _git(cfg.repo, "push", "-q", "origin", "main")


def run(cfg: WRehearsal) -> dict[str, Any]:
    started = time.monotonic()
    cfg.root.mkdir(parents=True, exist_ok=True)
    cfg.log(f"[{cfg.run}] preparing the practice monorepo under {cfg.root}")
    prepare(cfg)
    keys = cfg.root / "keys"
    keys.mkdir()
    signer = attest.Ed25519Signer.generate()  # a rehearsal key, never the T0 key (A-4)
    signer.save(keys / "attest-rehearsal", keys / "attest-rehearsal.pub")
    secret, recipient = sealed.new_identity()
    (keys / "sabotage.agekey").write_text(secret + "\n", encoding="ascii")
    saboteur = Saboteur(cfg.root / "sabotage-store", (recipient,), (HOST,))
    not_before = datetime.now(UTC) + timedelta(seconds=cfg.delay_s)
    spec = compose(SPEC_ID, "code.regression.exception_swallow", host=HOST, framing=FRAMING,
                   patch=PATCH, not_before=not_before.isoformat(), seed=4242,
                   ground_truth={"files": [FETCH], "symbols": ["fetch_all"],
                                 "mechanism": "a broad except returns an empty list"})  # fmt: skip
    record = saboteur.seal(spec, PATCH)
    cfg.log(f"[{cfg.run}] sealed {SPEC_ID}: c = {record['c'][:23]}…")
    log_path = cfg.repo / "sabotage-log" / f"{SPEC_ID}.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(canonicalize(record).decode("utf-8") + "\n", encoding="utf-8",
                        newline="\n")  # fmt: skip
    (cfg.repo / ".kintsugi" / "attest.pub").write_text(signer.public_key_openssh() + "\n",
                                                       encoding="utf-8", newline="\n")  # fmt: skip
    _commit(cfg.repo, "practice: the rehearsal attestation key and the sealed commitment")
    _git(cfg.repo, "push", "-q", "origin", "main")
    from kintsugi.control.genesis import write_genesis

    made = write_genesis(cfg.repo, github_repo=f"local/{cfg.run}", rulesets=[0])
    t0_commit = _commit(cfg.repo, f"T0: the window record of practice run {cfg.run}")
    _git(cfg.repo, "push", "-q", "origin", "main", "refs/heads/ledger")
    cfg.log(
        f"[{cfg.run}] T0 {made.window.t0}: genesis {made.genesis[:12]}…, T0 commit {t0_commit[:12]}"
    )
    runtime_file = cfg.root / "runtime.json"
    settings = {"repo": str(cfg.repo), "t0_key": str(keys / "attest-rehearsal"),
                "crew_model": "offline", "state": str(cfg.root / "state")}  # fmt: skip
    runtime_file.write_text(json.dumps(settings), encoding="utf-8")
    crew = build(RuntimeConfig.load(runtime_file))
    notary = Notary(signer, run=OfflineNotarySession(), require_checkpoint=False)
    pulls = LocalPulls(cfg.repo)
    lane = UpstreamLane(cfg.repo, notary, pulls, UpstreamConfig(poll_s=0, ci_timeout_s=0))
    crew.sabotage = SabotageLane(saboteur, secret, deliver=lane.deliver)  # pre-T0 seal: no publish
    step: Any = None
    crew.do_seal_sabotage(step, SPEC_ID)
    time.sleep(max(0.0, (not_before - datetime.now(UTC)).total_seconds()) + 0.5)
    crew.do_deliver_sabotage(step, SPEC_ID)
    cfg.log(f"[{cfg.run}] delivered through {pulls.pulls[1]['url']} ({pulls.pulls[1]['head']})")
    tag = crew.do_anchor_ledger(step, 0)
    cfg.log(f"[{cfg.run}] anchored {tag} and pushed the ledger")
    _git(cfg.repo, "checkout", "-q", "main")
    _git(cfg.repo, "pull", "-q", "--ff-only", "origin", "main")
    window = json.loads((cfg.repo / audit.WINDOW_FILE).read_text(encoding="utf-8"))
    window["t1"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    (cfg.repo / audit.WINDOW_FILE).write_text(json.dumps(window, indent=2) + "\n",
                                              encoding="utf-8", newline="\n")  # fmt: skip
    _spec, seal, _patch = saboteur.open(SPEC_ID, secret)
    path, body = Saboteur.reveal(spec, seal)
    (cfg.repo / path).write_bytes(body)
    _commit(cfg.repo, f"After W: t1 and the reveal of {SPEC_ID}")
    _git(cfg.repo, "push", "-q", "origin", "main")
    report = audit.run_audit(cfg.repo, selection=audit.Selection(checks=CHECKS))
    results = {r.check: r.as_dict() for r in report.results}
    cfg.log(f"[{cfg.run}] audit {', '.join(f'{k} {v["status"]}' for k, v in results.items())}")
    entries = crew.ledger.entries()
    if crew.ctx.store is not None:
        crew.ctx.store.close()  # release the run's SQLite files (the scratch tree is deletable)
    return summarize(
        cfg,
        entries,
        results,
        report.exit_code,
        tag,
        duration_s=time.monotonic() - started,
    )


def summarize(
    cfg: WRehearsal,
    entries: Sequence[Mapping[str, Any]],
    results: Mapping[str, Any],
    exit_code: int,
    tag: str,
    *,
    duration_s: float,
) -> dict[str, Any]:
    """Aggregate evidence: no key, no honeytoken, no patch."""
    return {
        "kind": "w-wiring-rehearsal", "run": cfg.run, "phase": "rehearsal", "model": "offline",
        "disclosed": ["practice host hosts/practice (not a fleet host)",
                      "practice spec S-P1 code.regression.exception_swallow (not S-17/18/19)",
                      "rehearsal attestation key (not the T0 key)",
                      "offline notary stand-in (git apply; no Entire checkpoint)",
                      "local pull requests (practice tests as CI; --no-ff merges)"],
        "ledger": [{"seq": e["seq"], "type": e.get("type"), "event": e.get("event")}
                   for e in entries],
        "anchor": tag, "audit": {"checks": list(CHECKS), "exit_code": exit_code,
                                 "results": dict(results)},
        "not_exercised": {"C2-C5": "Entire checkpoints from real sessions",
                          "C7,C12": "GitHub", "C8-C10": "neatlogs",
                          "C11,C14": "antibodies and self-repairs inside W",
                          "C13": "the practice host has no sealed holdout"},
        "duration_s": round(duration_s, 1),
    }  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The W wiring rehearsal (offline, disclosed).")
    parser.add_argument("--root", type=Path, required=True, help="scratch dir outside any repo")
    parser.add_argument("--run", default=datetime.now(UTC).strftime("w-%Y%m%d-%H%M%S"))
    parser.add_argument("--out", type=Path, help="write the aggregate summary here")
    args = parser.parse_args(argv)
    summary = run(WRehearsal(args.run, args.root / args.run))
    text = json.dumps(summary, indent=1, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
    print(text)
    return 0 if summary["audit"]["exit_code"] == audit.EXIT_OK else 1


if __name__ == "__main__":
    sys.exit(main())
