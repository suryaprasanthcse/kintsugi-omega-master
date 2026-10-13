"""The fleet runtime: auto-deploy from main and live traffic (spec §5.9, §6.2 step 4).

``kintsugi fleet up`` keeps a detached **deploy worktree** of main under ``.state/deploy`` and
fast-forwards it whenever main moves, so a merged change (a repair, a vaccination, or a
delivered sabotage) reaches live traffic without anyone deploying it. Each host receives live
tasks as a seeded Poisson stream at its ``tasks_per_hour`` (Ledger: 40/h), drawn from fresh
``live-<n>`` splits and run through the same fabric (LLM proxy and the background chaos
profile). A live task runs the **deployed** code: the child process gets the deploy worktree's
host packages first on ``PYTHONPATH``. Verdict rows are appended to
``.state/runtime/<host>.jsonl`` and the spans go to neatlogs, where the receptors watch them.

``kintsugi fleet down`` writes ``.state/runtime/STOP``; the runtime finishes its in-flight
tasks and exits. The 24-hour run is started from WSL2, never from inside the desktop app (D2).
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kintsugi.sandbox import proc
from kintsugi.sandbox.chaos_overlay import OVERLAY_DIR, ChaosOverlay

from .baseline import run_eval
from .fabric import Fabric, FabricConfig
from .hosts import HostSpec

STATE = Path(".state")
STOP_FILE = STATE / "runtime" / "STOP"


def _git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=120, check=True).stdout.strip()


@dataclass
class Deployer:
    """A detached worktree that follows ``ref`` (``origin/main`` in production)."""

    repo: Path
    path: Path
    ref: str = "origin/main"
    fetch: bool = True
    history: list[str] = field(default_factory=list)

    def head(self) -> str | None:
        return _git(self.path, "rev-parse", "HEAD") if (self.path / ".git").exists() else None

    def sync(self) -> str | None:
        """Bring the deploy to ``ref``; returns the new commit when it moved, else None."""
        if self.fetch and self.ref.startswith("origin/"):
            _git(self.repo, "fetch", "--quiet", "origin", self.ref.removeprefix("origin/"))
        target = _git(self.repo, "rev-parse", self.ref)
        current = self.head()
        if current == target:
            return None
        if current is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _git(self.repo, "worktree", "add", "--detach", str(self.path), target)
        else:
            _git(self.path, "checkout", "--quiet", "--detach", target)
        self.history.append(target)
        return target

    def pythonpath(self, packages: list[str]) -> str:
        roots = [self.path / "hosts" / name for name in packages]
        return os.pathsep.join(str(root) for root in roots)


def arrivals(rng: random.Random, rate_per_hour: float, scale: float = 1.0) -> Callable[[], float]:
    """Seconds until the next task of a Poisson stream (``scale`` speeds it up for drills)."""
    rate_per_s = rate_per_hour * scale / 3600.0
    return lambda: rng.expovariate(rate_per_s)


class Runtime:
    def __init__(
        self,
        repo: Path,
        hosts: Mapping[str, HostSpec],
        *,
        seed: int,
        deployer: Deployer | None,
        state_dir: Path = STATE,
        poll_s: float = 30.0,
        scale: float = 1.0,
        anthropic_key: str | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.repo = repo
        self.hosts = dict(hosts)
        self.seed = seed
        self.deployer = deployer
        self.state_dir = state_dir
        self.poll_s = poll_s
        self.scale = scale
        self.anthropic_key = anthropic_key
        self.extra_env = dict(extra_env or {})
        self.counts: dict[str, int] = dict.fromkeys(self.hosts, 0)
        self._lock = threading.Lock()

    def _env(self) -> dict[str, str]:
        env = dict(self.extra_env)
        if self.deployer is not None and self.deployer.head() is not None:
            env["PYTHONPATH"] = self.deployer.pythonpath(["hostkit", *self.hosts])
        return env

    def _live(self, fabric: Fabric, host: str, n: int) -> dict[str, Any]:
        split = f"live-{self.seed * 1_000_000 + n}"
        run = run_eval(fabric, self.hosts[host], split, self.seed, limit=1, workers=1,
                       extra_env=self._env())  # fmt: skip
        row = {**run.rows[0], "deploy": self.deployer.head() if self.deployer else None,
               "at": time.time()}  # fmt: skip
        out = self.state_dir / "runtime" / f"{host}.jsonl"
        with self._lock:
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row) + "\n")
            self.counts[host] += 1
        return row

    def run(self, *, duration_s: float | None = None, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        stop_file = self.state_dir / "runtime" / "STOP"
        stop_file.unlink(missing_ok=True)
        rng = random.Random(f"runtime:{self.seed}")
        next_gap = {h: arrivals(rng, s.economics.tasks_per_hour, self.scale)
                    for h, s in self.hosts.items()}  # fmt: skip
        started = time.monotonic()
        due = {h: started + gap() for h, gap in next_gap.items()}
        next_poll = started
        # Environment sabotage the crew delivers lands in the same state directory (§7.2).
        overlay = ChaosOverlay(self.state_dir / OVERLAY_DIR)
        config = FabricConfig(seed=self.seed, spend_path=self.state_dir / "spend.sqlite",
                              anthropic_key=self.anthropic_key, overlay=overlay)  # fmt: skip
        (self.state_dir).mkdir(parents=True, exist_ok=True)
        with Fabric(self.hosts, config) as fabric, cf.ThreadPoolExecutor(max_workers=4) as pool:
            n = 0
            while not stop.is_set() and not stop_file.exists():
                now = time.monotonic()
                if duration_s is not None and now - started >= duration_s:
                    break
                if self.deployer is not None and now >= next_poll:
                    self.deployer.sync()
                    next_poll = now + self.poll_s
                for host, when in due.items():
                    if now >= when:
                        n += 1
                        pool.submit(self._live, fabric, host, n)
                        due[host] = when + next_gap[host]()
                wake = min([*due.values(), next_poll if self.deployer else float("inf")])
                stop.wait(max(0.0, min(1.0, wake - time.monotonic())))
