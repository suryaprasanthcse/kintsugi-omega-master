"""L4 (§6.4): the main watcher polls ``origin/main`` and signals drift into open incidents.

``kintsugi control watch-main`` (started by ``scripts/w-runtime.sh``) fetches the remote branch,
reports each move once, never drops one whose signal failed, and its drift reaches a running
Incident through the FleetSupervisor, which re-verifies the round on the new main.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from kintsugi.cli import _build_parser
from kintsugi.control import contracts as c
from kintsugi.control import service as svc
from kintsugi.control.activities import CREW
from kintsugi.control.workflows import FleetSupervisor
from kintsugi.sandbox import proc
from kintsugi.senses.drift import Drift, MainWatcher
from tests.helpers.control import Scenario, crew_for, temporal


def _git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=60, check=True).stdout.strip()


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    """A bare origin, the runtime's checkout of it, and an upstream clone that pushes."""
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    origin = tmp_path / "origin.git"
    proc.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], timeout_s=30, check=True)
    upstream = tmp_path / "upstream"
    proc.run(["git", "clone", "-q", str(origin), str(upstream)], timeout_s=60, check=True)
    for args in (("user.name", "t"), ("user.email", "t@example.invalid")):
        _git(upstream, "config", *args)
    _git(upstream, "checkout", "-q", "-b", "main")
    (upstream / "a.txt").write_text("1\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "one")
    _git(upstream, "push", "-q", "origin", "main")
    checkout = tmp_path / "omega"
    proc.run(["git", "clone", "-q", str(origin), str(checkout)], timeout_s=60, check=True)
    return {"origin": origin, "upstream": upstream, "checkout": checkout, "tmp": tmp_path}


def _push(upstream: Path, text: str) -> str:
    (upstream / "a.txt").write_text(text, encoding="utf-8")
    _git(upstream, "commit", "-q", "-am", text.strip())
    _git(upstream, "push", "-q", "origin", "main")
    return _git(upstream, "rev-parse", "HEAD")


def test_the_watcher_fetches_origin_main_and_reports_each_move_once(world: dict[str, Path]) -> None:
    seen: list[Drift] = []
    state = world["tmp"] / "heads.json"
    watcher = MainWatcher({"omega": world["checkout"]}, seen.append, state_path=state,
                          remote="origin")  # fmt: skip
    assert watcher.poll_once() == []  # the first poll records the head
    old = _git(world["checkout"], "rev-parse", "origin/main")
    new = _push(world["upstream"], "2\n")  # pushed upstream; nobody pulls the checkout
    assert watcher.poll_once() == [Drift("omega", old, new)]
    assert _git(world["checkout"], "cat-file", "-t", new) == "commit"  # there for the graph diff
    assert watcher.poll_once() == []
    assert seen == [Drift("omega", old, new)]


def test_a_drift_whose_signal_failed_is_reported_again(world: dict[str, Path]) -> None:
    calls: list[Drift] = []

    def flaky(drift: Drift) -> None:
        calls.append(drift)
        if len(calls) == 1:
            raise ConnectionError("temporal is restarting")

    state = world["tmp"] / "heads.json"
    watcher = MainWatcher({"omega": world["checkout"]}, flaky, state_path=state, remote="origin")
    watcher.poll_once()
    new = _push(world["upstream"], "3\n")
    with pytest.raises(ConnectionError):
        watcher.poll_once()
    restarted = MainWatcher({"omega": world["checkout"]}, flaky, state_path=state,
                            remote="origin")  # fmt: skip
    assert [d.new_main for d in restarted.poll_once()] == [new]  # not lost across a restart
    assert [d.new_main for d in calls] == [new, new]


def test_the_cli_and_the_launcher_run_the_watcher() -> None:
    args = _build_parser().parse_args(["control", "watch-main", "--target", "127.0.0.1:7233",
                                       "--remote", "origin", "--interval", "30"])  # fmt: skip
    assert (args.control_command, args.remote, args.branch) == ("watch-main", "origin", "main")
    launcher = (Path(__file__).resolve().parents[2] / "scripts" / "w-runtime.sh").read_text(
        encoding="utf-8")  # fmt: skip
    assert "spawn watch-main uv run kintsugi control watch-main" in launcher
    assert "SERVICES=(temporal watchdog forward watch-main ingress poller fleet)" in launcher


def test_drift_on_origin_main_reaches_the_open_incident_through_the_supervisor(
    world: dict[str, Path],
) -> None:
    rebased: list[tuple[str, str]] = []

    async def scenario() -> dict[str, Any]:
        gate = threading.Event()
        crew = crew_for(Scenario(passing={0: 1}, gate=gate))
        crew.answers["rebase_and_rerun"] = lambda outs, repo, new: rebased.append((repo, new))
        config = c.ControlConfig(canary_interval_s=1.0, supervisor_id="sup-l4")
        state = world["tmp"] / "main-watch.json"
        async with temporal(crew) as (_env, client):
            sup = await client.start_workflow(FleetSupervisor.run, args=[config, None],
                                              id=config.supervisor_id, task_queue=CREW)  # fmt: skip

            async def signal(repo: str, new_main: str) -> None:
                await sup.signal(FleetSupervisor.drift, args=[repo, new_main])

            async def watch() -> int:
                return await svc.watch_main("unused", {"omega": world["checkout"]},
                                            config.supervisor_id, state_path=state, once=True,
                                            signal=signal, log=lambda line: None)  # fmt: skip

            assert await watch() == 0  # records origin/main before any incident exists
            await sup.signal(
                FleetSupervisor.hit,
                c.Hit("k1", "det", "trace-1", "span", receptor="R-007", host="ledger"),
            )
            deadline = time.monotonic() + 30
            while crew.count("surgeon") == 0:  # a round is in flight
                assert time.monotonic() < deadline
                await asyncio.sleep(0.05)
            new = _push(world["upstream"], "4\n")  # S-19's shape: main moves mid-repair
            assert await watch() == 1
            gate.set()

            async def closed() -> bool:
                return len((await sup.query(FleetSupervisor.status))["closed"]) == 1

            while not await closed():
                assert time.monotonic() < deadline + 30
                await asyncio.sleep(0.1)
            final = await sup.query(FleetSupervisor.status)
            await sup.terminate("test over")
        return {"new": new, "final": final}

    out = asyncio.run(scenario())
    assert out["final"]["closed"] == {"INC-R-007-ledger": "RESOLVED"}
    assert ("omega", out["new"]) in rebased  # the incident re-verified on the new main
