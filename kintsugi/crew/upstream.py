"""The upstream lane: the Saboteur's ``deliver`` and ``publish`` ports (Stage 13a; §6.2 step 3).

A sabotage reaches the fleet the way any upstream change does, never through Omega's gates:

1. **Base**: the remote's current ``main`` is fetched into ``refs/kintsugi/base/<record>``.
2. **Commit**: a disposable worktree off that base, and a notary session applies the request's
   patch there (``kintsugi.crew.notary``): an attested commit with an Entire checkpoint, so the
   delivery itself satisfies C4. ``deliver`` uses the host's package as the session's root.
3. **Ship**: the commit is pushed as ``upstream/<host>-<UTC minute>`` (``publish``:
   ``upstream/seal-<UTC minute>``); a pull request opens against the base; it merges with a merge
   commit (A-51) once the repository's **normal CI** on the pull request is green. Omega's
   G0–G7 do not apply to the upstream lane (§6.2 step 3).

Branch names, titles and bodies carry only the request's own fields, which
``Saboteur.delivery_request`` has already checked for leaks (A-68): never the sabotage id. The
ledger's ``sabotage/delivered`` entry is written by the crew step after ``deliver`` returns, so
C6 sees the seal before the delivery.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kintsugi.crew.notary import Notary, NotaryRequest
from kintsugi.sandbox import proc, worktree


class UpstreamError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpstreamConfig:
    base_branch: str = "main"
    remote: str = "origin"
    branch_prefix: str = "upstream/"
    checks: tuple[str, ...] = ()  # check names that must pass; empty: every reported check
    ci_timeout_s: float = 3600.0
    poll_s: float = 20.0
    merge_method: str = "merge"  # A-51
    host_root: str = "hosts/{host}"  # the notary session's package root for a delivery


@dataclass(frozen=True)
class Landed:
    sha: str  # the attested commit
    checkpoint: str | None
    branch: str
    pr_url: str
    merge_sha: str


def _git(repo: Path, *args: str, timeout_s: float = 300.0) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=timeout_s, check=True).stdout


def ci_verdict(checks: Sequence[dict[str, Any]], required: Sequence[str]) -> bool | None:
    """True when every relevant check passed, False when one failed, None while pending.
    Skipped jobs are no verdict either way; without required names every check counts."""
    relevant = [k for k in checks if str(k.get("bucket")) != "skipping"
                and (not required or any(r in str(k.get("name")) for r in required))]  # fmt: skip
    if not relevant:
        return None
    if required and not all(any(r in str(k.get("name")) for k in relevant) for r in required):
        return None  # a required check has not reported yet
    if any(str(k.get("bucket")) in ("fail", "cancel") for k in relevant):
        return False
    if all(str(k.get("bucket")) == "pass" for k in relevant):
        return True
    return None


class UpstreamLane:
    """``deliver(request, host)`` and ``publish(request)`` for :class:`SabotageLane`."""

    def __init__(
        self,
        repo: Path,
        notary: Notary,
        github: Any,  # kintsugi.control.ship.GhCli (or a fake)
        config: UpstreamConfig | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = Path(repo)
        self.notary = notary
        self.github = github
        self.config = config or UpstreamConfig()
        self.sleep = sleep
        self.clock = clock

    def deliver(self, request: NotaryRequest, host: str) -> str:
        landed = self.land(request, f"{host}-{self._stamp()}",
                           package_root=self.config.host_root.format(host=host))  # fmt: skip
        return landed.pr_url

    def publish(self, request: NotaryRequest) -> str:
        return self.land(request, f"seal-{self._stamp()}", package_root=".").merge_sha

    def _stamp(self) -> str:
        return self.clock().strftime("%Y%m%d%H%M")

    def _base(self, record: str) -> str:
        cfg = self.config
        ref = f"refs/kintsugi/base/{record}"
        _git(self.repo, "fetch", "-q", cfg.remote, f"+refs/heads/{cfg.base_branch}:{ref}")
        return _git(self.repo, "rev-parse", ref).strip()

    def land(self, request: NotaryRequest, slug: str, *, package_root: str) -> Landed:
        cfg = self.config
        base = self._base(request.incident)
        wt = worktree.create(self.repo, request.incident, slug, base=base)
        try:
            notarised = self.notary.commit(wt.path, request, package_root=package_root)
        finally:
            worktree.release_lock(wt)
        branch = cfg.branch_prefix + slug
        _git(self.repo, "push", cfg.remote, f"{notarised.sha}:refs/heads/{branch}")
        gh = self.github
        body = (f"{request.purpose}\n\nCommitted by an attested notary session (checkpoint "
                f"`{notarised.checkpoint_id or 'n/a'}`).")  # fmt: skip
        pr = gh.pr_find(branch) or gh.pr_create(cfg.base_branch, branch, request.subject[:120],
                                                body)  # fmt: skip
        number, url = int(pr["number"]), str(pr.get("url", ""))
        verdict = self._wait_ci(number)
        if verdict is not True:
            state = "red" if verdict is False else "not green in time"
            raise UpstreamError(f"{url}: the repository's CI is {state}")
        merged = gh.pr_merge(number, cfg.merge_method)
        if merged is None:
            raise UpstreamError(f"{url}: the merge was refused")
        worktree.discard(wt)
        return Landed(notarised.sha, notarised.checkpoint_id, branch, url, merged)

    def _wait_ci(self, number: int) -> bool | None:
        deadline = time.monotonic() + self.config.ci_timeout_s
        while True:
            verdict = ci_verdict(self.github.pr_checks(number), self.config.checks)
            if verdict is not None or time.monotonic() > deadline:
                return verdict
            self.sleep(self.config.poll_s)
