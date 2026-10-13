"""Upstream drift: the main-branch watcher and L4 re-verification (spec §6.4).

When a host's main moves while a repair is in flight (S-19, "benign upstream drift"):

1. :class:`MainWatcher` sees the new head (a 30-second poll of ``origin/main`` in W, run by
   ``kintsugi control watch-main`` from ``scripts/w-runtime.sh``; a GitHub push webhook can call
   :meth:`MainWatcher.poll_once` too) and the control plane signals ``drift(repo, new_main)``
   into the FleetSupervisor, which forwards it to every open incident.
2. :func:`reverify_plan` decides what must be re-checked for a candidate fix:
   changed entities = ``entire graph diff old_main..new_main``; patched entities =
   ``entire graph diff old_main..fix``; the blast radius of the changed entities is
   ``entire graph impact`` (callers, callees, type consumers, data flows, co-changes); when it
   intersects the patched entities, G4 is re-run, otherwise its result is reused with the
   justification recorded. G1 and G2 always re-run (cheap); G3 re-runs only the tests inside the
   impact radius; G7 always runs on the final head.

The record (``{"G1": "rerun", "G3": "rerun:7", "G4": "rerun:intersection{run_reconcile}", …}``)
goes into the incident's case file and the proof bundle.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from kintsugi.memory import graph as graph_cli
from kintsugi.sandbox import proc

IMPACT_SECTIONS = ("callers", "callees", "type_consumers", "data_flows", "co_changes", "siblings")


@dataclass(frozen=True)
class Drift:
    repo: str  # the repository id the control plane knows (a host id or a path)
    old_main: str
    new_main: str


class MainWatcher:
    """Polls each repository's branch head; reports every change once (state persisted).

    With ``remote`` set (W: ``origin``), each poll fetches ``<remote>/<branch>`` first and reads
    that ref, so a change pushed to GitHub is seen even though nobody pulls the checkout, and the
    new head exists locally for the L4 graph diff. A head is recorded only after ``on_drift``
    returned: a signal that failed is reported again on the next poll, never lost.
    """

    def __init__(
        self,
        repos: Mapping[str, Path],
        on_drift: Callable[[Drift], None],
        *,
        branch: str = "main",
        state_path: Path | None = None,
        remote: str | None = None,
    ) -> None:
        self.repos = {name: Path(path) for name, path in repos.items()}
        self.on_drift = on_drift
        self.branch = branch
        self.remote = remote
        self.state_path = state_path
        self.seen: dict[str, str] = {}
        if state_path is not None and state_path.is_file():
            self.seen = dict(json.loads(state_path.read_text(encoding="utf-8")))

    @property
    def ref(self) -> str:
        return f"{self.remote}/{self.branch}" if self.remote else self.branch

    def head(self, repo: Path) -> str:
        if self.remote:
            proc.run(["git", "-C", str(repo), "fetch", "--quiet", "--no-tags", self.remote,
                      f"+refs/heads/{self.branch}:refs/remotes/{self.ref}"],
                     timeout_s=120, check=True)  # fmt: skip
        result = proc.run(["git", "-C", str(repo), "rev-parse", "--verify", self.ref],
                          timeout_s=30, check=True)  # fmt: skip
        return result.stdout.strip()

    def poll_once(self) -> list[Drift]:
        drifts = []
        try:
            for name, path in sorted(self.repos.items()):
                head = self.head(path)
                previous = self.seen.get(name)
                if previous is not None and previous != head:
                    drift = Drift(name, previous, head)
                    self.on_drift(drift)
                    drifts.append(drift)
                self.seen[name] = head
        finally:
            if self.state_path is not None:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps(self.seen, sort_keys=True),
                                           encoding="utf-8")  # fmt: skip
        return drifts

    def run(self, stop: threading.Event, *, interval_s: float = 30.0) -> None:
        while not stop.is_set():
            self.poll_once()
            stop.wait(interval_s)


# ---- the L4 re-verification policy -----------------------------------------------------------


class GraphPort(Protocol):
    def diff(self, repo: Path, base: str, head: str) -> dict[str, Any]: ...

    def impact(self, repo: Path, symbol: str) -> dict[str, Any]: ...


class EntireGraph:
    """The real graph, through ``kintsugi.memory.graph``."""

    def diff(self, repo: Path, base: str, head: str) -> dict[str, Any]:
        return graph_cli.diff(repo, base, head)

    def impact(self, repo: Path, symbol: str) -> dict[str, Any]:
        return graph_cli.impact(repo, symbol, depth=2, limit=25, exclude_tests=False)


def changed_entities(diff: Mapping[str, Any]) -> set[str]:
    """Entity names an ``entire graph diff --json`` reports (renames contribute both names)."""
    names: set[str] = set()
    for entry in diff.get("files") or []:
        for change in entry.get("changes") or []:
            for key in ("name", "old_name", "new_name"):
                if change.get(key):
                    names.add(str(change[key]))
    return names


def impact_endpoints(impact: Mapping[str, Any]) -> list[dict[str, Any]]:
    found = []
    focus = impact.get("focus")
    if isinstance(focus, dict):
        found.append(focus)
    for section in IMPACT_SECTIONS:
        for entry in (impact.get(section) or {}).get("entries") or []:
            endpoint = entry.get("endpoint")
            if isinstance(endpoint, dict):
                found.append(endpoint)
    return found


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return "tests" in parts or parts[-1].startswith("test_")


@dataclass(frozen=True)
class Reverify:
    old_main: str
    new_main: str
    head: str
    changed: tuple[str, ...]
    patched: tuple[str, ...]
    impacted: tuple[str, ...]
    intersection: tuple[str, ...]
    tests: tuple[str, ...] = field(default=())  # test functions inside the impact radius

    @property
    def rerun_g4(self) -> bool:
        return bool(self.intersection)

    def record(self) -> dict[str, str]:
        g4 = (f"rerun:intersection{{{','.join(self.intersection)}}}" if self.intersection
              else "reuse:no-intersection")  # fmt: skip
        return {"G1": "rerun", "G2": "rerun",
                "G3": f"rerun:{len(self.tests)}" if self.tests else "rerun:all",
                "G4": g4, "G7": "final-head"}  # fmt: skip


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def reverify_plan(
    repo: Path,
    old_main: str,
    new_main: str,
    head: str,
    *,
    graph: GraphPort | None = None,
) -> Reverify:
    port = graph or EntireGraph()
    changed = changed_entities(port.diff(repo, old_main, new_main))
    patched = changed_entities(port.diff(repo, old_main, head))
    impacted: set[str] = set(changed)
    tests: set[str] = set()
    for name in sorted(changed):
        try:
            report = port.impact(repo, name)
        except graph_cli.GraphError:
            continue  # a symbol that no longer exists has no radius of its own
        for endpoint in impact_endpoints(report):
            qualified = str(endpoint.get("qualified_name") or endpoint.get("name") or "")
            if _is_test(str(endpoint.get("file_path") or "")):
                tests.add(qualified)
            elif qualified:
                impacted.add(qualified)
    short_patched = {_short(n) for n in patched}
    hits = {n for n in impacted if n in patched or _short(n) in short_patched}
    return Reverify(old_main, new_main, head, _sorted(changed), _sorted(patched),
                    _sorted(impacted), _sorted(hits), _sorted(tests))  # fmt: skip


def _sorted(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))
